#
#  Copyright (c) 2023-2026 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""The AwaitingOn message (service protocol V7): what a handler awaits, sent before the SDK blocks on input.

Restate may suspend an invocation that told it what it awaits, so the message
must reach Restate while the SDK waits, not with whatever Restate sends next.
These drive a handler through the ASGI app, with the test playing Restate on
the wire: no Restate server.
"""

# pylint: disable=missing-function-docstring

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from typing import Union

import pytest

import restate
from restate.server_types import ASGIReceiveEvent, ASGISendEvent, Scope
from sessions_wire import TYPE, Message, bytes_, decode, frame, messages, uint, value  # pylint: disable=import-error

V7 = "application/vnd.restate.invocation.v7"

SUSPENSION = 0x0001
PROPOSE_RUN_COMPLETION = 0x0005
SLEEP = 0x040C
RUN = 0x0411

CANCEL_SIGNAL_ID = 1
FIRST_COMPLETED = 1

IDLE_TURNS = 50
"""Turns of the event loop after which an attempt that got nothing new has written all it would: a few do."""


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [
    pytest.mark.anyio,
]


class Restate:
    """Restate's side of one invocation: the start and the input, then only what a test sends."""

    def __init__(self) -> None:
        self._inbound: asyncio.Queue[ASGIReceiveEvent] = asyncio.Queue()
        self._chunks: list[bytes] = []
        self.ended = False

    async def receive(self) -> ASGIReceiveEvent:
        return await self._inbound.get()

    async def send(self, event: ASGISendEvent) -> None:
        if event["type"] == "http.response.body":
            if event.get("body"):
                self._chunks.append(bytes(event["body"]))
            if not event.get("more_body", False):
                self.ended = True

    def start(self) -> None:
        """Start the invocation, with no input: the start message, and the input command."""
        self._inbound.put_nowait(
            {
                "type": "http.request",
                "body": frame(TYPE.start, bytes_(1, b"\x01\x02\x03\x04") + bytes_(2, "inv_test") + uint(3, 1))
                + frame(TYPE.input, bytes_(14, value(json.dumps(None)))),
                "more_body": True,
            }
        )

    def close_input(self) -> None:
        self._inbound.put_nowait({"type": "http.request", "body": b"", "more_body": False})

    def hang_up(self) -> None:
        """End the request and the connection: what lets an attempt that is still going end."""
        self.close_input()
        self._inbound.put_nowait({"type": "http.disconnect"})

    def sent(self) -> list[Message]:
        return messages(b"".join(self._chunks))

    def types(self) -> list[int]:
        return [m.type for m in self.sent()]

    async def until(self, condition: Callable[[], bool], what: str, timeout: float = 5.0) -> None:
        """Return once ``condition`` holds, Restate sending nothing meanwhile; fail after ``timeout``."""
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() >= deadline:
                raise AssertionError(f"waited {timeout}s for {what}; sent: {[hex(t) for t in self.types()]}")
            await asyncio.sleep(0.001)


@contextlib.asynccontextmanager
async def invoked(service: restate.Service, handler: str) -> AsyncIterator[Restate]:
    """Invoke ``service``'s ``handler`` through the ASGI app, at protocol V7, with the test playing Restate."""
    app = restate.app([service])
    scope: Scope = {
        "type": "http",
        "asgi": {"spec_version": "2.3", "version": "3.0"},
        "http_version": "2",
        "method": "POST",
        "scheme": "http",
        "path": f"/invoke/{service.name}/{handler}",
        "raw_path": f"/invoke/{service.name}/{handler}".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", V7.encode())],
        "client": None,
        "server": None,
        "extensions": None,
    }
    side = Restate()
    side.start()
    serving = asyncio.ensure_future(app(scope, side.receive, side.send))
    try:
        yield side
        await asyncio.wait_for(serving, 5)
    finally:
        if not serving.done():
            # A test that failed midway: the SDK swallows a cancellation of its handler, then waits for the end
            # of the request, so it gets that too.
            side.hang_up()
            serving.cancel()
            await asyncio.wait({serving}, timeout=5)


def _varints(raw: Union[int, bytes]) -> list[int]:
    """The values of a repeated varint field: one, or packed in bytes."""
    if isinstance(raw, int):
        return [raw]
    out, at = [], 0
    while at < len(raw):
        n, shift = 0, 0
        while True:
            byte = raw[at]
            at += 1
            n |= (byte & 127) << shift
            shift += 7
            if byte < 128:
                break
        out.append(n)
    return out


def awaited(message: Message) -> tuple[list[int], list[int], int, bool]:
    """An AwaitingOn message's future, flat: its completions, signals and combinator; and executing_side_effects."""
    assert message.type == TYPE.awaiting_on
    future = decode(message.bytes_of(1))
    completions = [n for raw in future.get(1, []) for n in _varints(raw)]
    signals = [n for raw in future.get(2, []) for n in _varints(raw)]
    combinator = future.get(5, [0])[0]
    assert isinstance(combinator, int)
    return completions, signals, combinator, bool(message.fields.get(2, [0])[0])


async def idle() -> None:
    for _ in range(IDLE_TURNS):
        await asyncio.sleep(0)


async def test_the_await_is_sent_before_blocking_on_input():
    sleeper = restate.Service("sleeper")

    @sleeper.handler()
    async def nap(ctx: restate.Context) -> None:
        await ctx.sleep(timedelta(hours=1))

    async with invoked(sleeper, "nap") as side:
        # Restate sends nothing more: the handler waits on the sleep, and the SDK on input. What it waits on is
        # on the wire all the same.
        await side.until(lambda: TYPE.awaiting_on in side.types(), "the AwaitingOn message")
        assert side.types() == [SLEEP, TYPE.awaiting_on]
        # The sleep's completion, or the cancellation; nothing of it is a run the SDK executes.
        assert awaited(side.sent()[-1]) == ([1], [CANCEL_SIGNAL_ID], FIRST_COMPLETED, False)

        side.close_input()
        await side.until(lambda: side.ended, "the end of the response")
        assert side.types() == [SLEEP, TYPE.awaiting_on, SUSPENSION]


async def test_no_await_is_sent_while_a_run_executes_and_one_is_after_its_proposal():
    worker = restate.Service("worker")
    started = asyncio.Event()
    gate = asyncio.Event()

    async def step() -> str:
        started.set()
        await gate.wait()
        return "done"

    @worker.handler()
    async def work(ctx: restate.Context) -> str:
        return await ctx.run_typed("step", step)

    async with invoked(worker, "work") as side:
        await asyncio.wait_for(started.wait(), 5)
        await idle()
        # The handler waits on the run's result, which the SDK is producing: nothing to tell Restate.
        assert side.types() == [RUN]

        gate.set()
        # The result is proposed, and the handler waits for Restate to store it.
        await side.until(lambda: TYPE.awaiting_on in side.types(), "the AwaitingOn message")
        assert side.types() == [RUN, PROPOSE_RUN_COMPLETION, TYPE.awaiting_on]
        assert awaited(side.sent()[-1]) == ([1], [CANCEL_SIGNAL_ID], FIRST_COMPLETED, False)

        side.close_input()
        await side.until(lambda: side.ended, "the end of the response")
        assert side.types() == [RUN, PROPOSE_RUN_COMPLETION, TYPE.awaiting_on, SUSPENSION]
