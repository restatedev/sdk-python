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
"""The call records of Restate tools ride the commits that happen anyway (``_calls.py``): that a call answered, and that
its record is forgotten, are stored with the commit of the call's result, not by commits of their own. What that
must keep: a stopped turn closed by another invocation reports a call that answered as having happened, and a crash
at any point of the turn makes the call once."""

# pylint: disable=missing-function-docstring,missing-class-docstring,import-error,import-outside-toplevel

import asyncio
import typing
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

import pytest

from restate import sessions
from restate.agents._common import ENDED, RETURNED_UNRECORDED
from restate.agents._conversation import Conversation
from restate.agents._conversation import settle as settle_turn
from restate.agents._tools import Tool
from restate.sessions.internal import invoke
from agents_commits import (
    SHAPE,
    SLOW,
    TURN,
    Counting,
    adk_ask,
    held,
    restated_lookup,
    slow_tool,
    until_done,
)
from sessions_database import Backend
from sessions_object import Host, request


@pytest.fixture
def anyio_backend():
    return "asyncio"


@dataclass
class Watched(Counting):
    """A counting host that tells a test when a call is made and when a commit has stored a text, and crashes then."""

    on_call: typing.Callable[["Watched"], None] | None = None
    crash_once_stored: str | None = None

    def generic_call(self, service, handler, arg, key=None, idempotency_key=None, headers=None, name=None):
        if handler == "status" and self.on_call is not None:
            self.on_call(self)
        return super().generic_call(service, handler, arg, key, idempotency_key, headers, name)

    async def sync(self) -> None:
        await super().sync()
        if self.crash_once_stored is not None:
            text, _ = await held(self.committed[-1])
            if self.crash_once_stored in text:
                self.crash_once_stored = None
                self.crash_after(0)


# --- A turn killed while a slow tool runs ----------------------------------------------------------------------


async def read_results(backend: Backend) -> list[tuple[str, str]]:
    """The tool results of the key's transcript, in order: (tool call id, output)."""
    from restate.agents.adk._agent import _Items  # pyright: ignore[reportPrivateUsage]

    reader = sessions.SessionObject("asker")

    @reader.handler
    async def read(ctx: sessions.SessionContext) -> list[tuple[str, str]]:
        conversation = await Conversation.load(ctx.db, _Items("assistant"), complete=True)
        return [(r.id, r.output) for e in conversation.entries for r in e.results]

    return await invoke(reader, "read", Host(backend, service="asker"), request())


async def test_a_turn_killed_while_a_slow_tool_runs_is_closed_by_another_invocation_with_the_call_reported_done():
    from restate.agents.adk._agent import _Items  # pyright: ignore[reportPrivateUsage]

    SHAPE[0] = "parallel"
    try:
        ask = await adk_ask()
        asker = sessions.SessionObject("asker")

        @asker.handler
        async def run(ctx: sessions.SessionContext, text: str) -> str:
            return await ask(ctx, text, [restated_lookup, slow_tool])

        @asker.handler(name="settle")
        async def settling(ctx: sessions.SessionContext, invocation_id: str) -> bool:
            return await settle_turn(ctx, _Items("assistant"), invocation_id)

        TURN[0] = "killed"
        SLOW[:] = [asyncio.Event()]
        gate = asyncio.Event()
        backend, req = Backend(), request()
        host = Counting(backend, service="asker", answers={"publish": b'{"revision": 1}'}, gate=gate)
        turn = asyncio.ensure_future(invoke(asker, "run", host, req, "killed"))
        try:
            # Call 1 answered while the slow tool runs: its answer is written on its own, at once.
            await until_done(host, {0})
            # The process dies here: nothing more is stored, and the slow tool never ends.
            host.crash_after(0)
        finally:
            turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)
        other = Host(backend, service="asker", answers={"publish": b'{"revision": 1}'})
        assert await invoke(asker, "settle", other, request(), req.id) is True
        results = dict(await read_results(backend))
        assert results["call-killed-0"] == RETURNED_UNRECORDED
        assert results["call-killed-1"] == ENDED
        assert results["call-killed-2"] == ENDED
    finally:
        SHAPE[0] = "serial"


# --- A crash ---------------------------------------------------------------------------------------------------


async def crashing_ask(rounds: int) -> Any:
    """ADK's agent whose model asks for ``rounds`` calls of the same tool, one after each result, as the transcript
    says (so that every attempt asks the same), and then answers."""
    from google.adk.models import BaseLlm
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    from restate.agents import adk

    class ByResults(BaseLlm):
        model: str = "scripted"

        async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
        ) -> AsyncGenerator[LlmResponse, None]:
            results = sum(1 for c in llm_request.contents for p in c.parts or [] if p.function_response)
            if results >= rounds:
                part = types.Part(text="done")
            else:
                call = types.FunctionCall(id=f"call_{results + 1}", name="lookup", args={"key": "eu", "input": "o1"})
                part = types.Part(function_call=call)
            yield LlmResponse(content=types.Content(role="model", parts=[part]))

    async def ask(ctx: sessions.SessionContext, text: str, tools: list[Tool]) -> str:
        return await adk.prompt(ctx, text, model=ByResults(), tools=[*tools])

    return ask


def keys_of(*hosts: Host) -> list[str | None]:
    return [c[4] for host in hosts for c in host.calls if c[1] == "status"]


async def records_of(backend: Backend) -> list[dict[str, Any]]:
    return (await held(dict(backend.stored)))[1]


async def test_a_crash_after_a_call_answered_and_before_its_result_is_stored_makes_it_again_under_the_same_key():
    ask = await crashing_ask(1)
    asker = sessions.SessionObject("asker")

    @asker.handler
    async def run(ctx: sessions.SessionContext, text: str) -> str:
        return await ask(ctx, text, [restated_lookup])

    TURN[0] = "crash"
    backend, req = Backend(), request()
    published = {"publish": b'{"revision": 1}'}
    # The process dies as the call returns: the commit that would store its answer and its result fails.
    first = Watched(backend, service="asker", answers=dict(published), on_call=lambda host: host.crash_after(0))
    with pytest.raises(Exception):
        await invoke(asker, "run", first, req, "go")
    assert not any(r.get("done") for r in await records_of(backend))
    retry = Watched(backend, service="asker", answers=dict(published))
    assert await invoke(asker, "run", retry, req, "go") == "done"
    assert keys_of(first, retry) == [f"{req.id}:call_1", f"{req.id}:call_1"]
    assert [i for i, _ in await read_results(backend)] == ["call_1"]


async def test_a_crash_right_after_a_result_is_stored_forgets_the_record_and_a_later_identical_call_is_a_new_call():
    ask = await crashing_ask(2)
    asker = sessions.SessionObject("asker")

    @asker.handler
    async def run(ctx: sessions.SessionContext, text: str) -> str:
        return await ask(ctx, text, [restated_lookup])

    TURN[0] = "crash"
    backend, req = Backend(), request()
    published = {"publish": b'{"revision": 1}'}
    # The commit that stores the result is the crash's last: the next write fails.
    first = Watched(backend, service="asker", answers=dict(published), crash_once_stored="shipped o1")
    with pytest.raises(Exception):
        await invoke(asker, "run", first, req, "go")
    # Stored with the result: the call answered, and its forgetting has not been written.
    assert [i for i, _ in await read_results(backend)] == ["call_1"]
    records = await records_of(backend)
    assert [(r["ids"], r.get("done")) for r in records] == [(["call_1"], True)]
    retry = Watched(backend, service="asker", answers=dict(published))
    assert await invoke(asker, "run", retry, req, "go") == "done"
    # The same tool and input again, under a new id: not the call the model has the result of.
    assert keys_of(first, retry) == [f"{req.id}:call_1", f"{req.id}:call_2"]
    assert [i for i, _ in await read_results(backend)] == ["call_1", "call_2"]
