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
"""The agents' companion object, signals, against a real Restate server."""

# pylint: disable=missing-function-docstring

import asyncio
import typing
import uuid
from collections.abc import Awaitable, Callable

import httpx2
import pytest

import restate
from restate import HarnessEnvironment
from restate.agents import _signals
from restate.agents._api import Signal, Status, Turn, Watch
from restate.agents._signals import WaitRequest


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

# The agent's side: its handlers call the companion's internal handlers, which the ingress refuses.
driver = restate.Service("driver")


@driver.handler()
async def start_turn(ctx: restate.Context, request: dict[str, str]) -> None:
    turn: Turn = {"invocationId": request["invocation"], "since": "2026-11-04T00:00:00.000Z"}
    await ctx.object_call(_signals.start, key=request["key"], arg=turn)


@driver.handler()
async def close_turn(ctx: restate.Context, request: dict[str, str]) -> None:
    await ctx.object_call(_signals.close, key=request["key"], arg=request["invocation"])


failing = restate.Service("failing")


@failing.handler()
async def fail(ctx: restate.Context) -> None:
    raise restate.TerminalError("this turn was killed")


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        restate.app([_signals.signals, driver, failing]), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


async def attach(harness: HarnessEnvironment, invocation_id: str) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=60) as ingress:
        return await ingress.get(f"{harness.ingress_url}/restate/invocation/{invocation_id}/attach")


async def until(check: Callable[[], Awaitable[bool]]) -> None:
    for _ in range(100):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("never")


def key() -> str:
    return f"assistant:{uuid.uuid4()}"


def waiting(wait_id: str, name: str) -> WaitRequest:
    return {"id": wait_id, "name": name}


def watching(revision: int, timeout: float) -> Watch:
    return {"revision": revision, "timeoutSeconds": timeout}


def signal(**fields: typing.Any) -> Signal:
    return typing.cast(Signal, fields)


async def test_a_wait_returns_the_value_of_the_signal_that_resolves_it(harness: HarnessEnvironment):
    k = key()
    sent = await harness.client.object_send(_signals.wait, key=k, arg=waiting("inv:1", "approval"))

    async def listed() -> bool:
        return len(await harness.client.object_call(_signals.pending, key=k, arg=None)) == 1

    await until(listed)
    [pending] = await harness.client.object_call(_signals.pending, key=k, arg=None)
    assert pending["id"] == "inv:1" and pending["name"] == "approval" and pending["since"].endswith("Z")
    resolved = await harness.client.object_call(
        _signals.resolve, key=k, arg=signal(**{"id": "inv:1", "value": "approved"})
    )
    assert resolved == {"resolved": "inv:1"}
    answer = await attach(harness, sent.invocation_id)
    assert answer.json() == "approved"
    assert await harness.client.object_call(_signals.pending, key=k, arg=None) == []


async def test_a_signal_resolves_the_oldest_wait_of_its_name_or_is_dropped_or_kept(harness: HarnessEnvironment):
    k = key()
    # No one waits: dropped, unless kept for the next wait.
    assert await harness.client.object_call(_signals.resolve, key=k, arg=signal(**{"name": "go", "value": 1})) == {
        "resolved": None
    }
    await harness.client.object_call(_signals.resolve, key=k, arg=signal(**{"name": "go", "value": 2, "keep": True}))
    kept = await harness.client.object_send(_signals.wait, key=k, arg=waiting("inv:2", "go"))
    assert (await attach(harness, kept.invocation_id)).json() == 2

    first = await harness.client.object_send(_signals.wait, key=k, arg=waiting("inv:3", "go"))
    second = await harness.client.object_send(_signals.wait, key=k, arg=waiting("inv:4", "go"))

    async def both() -> bool:
        return len(await harness.client.object_call(_signals.pending, key=k, arg=None)) == 2

    await until(both)
    assert await harness.client.object_call(_signals.resolve, key=k, arg=signal(**{"name": "go", "value": "a"})) in (
        {"resolved": "inv:3"},
        {"resolved": "inv:4"},
    )
    await harness.client.object_call(_signals.resolve, key=k, arg=signal(**{"name": "go", "value": "b"}))
    answers = {(await attach(harness, w.invocation_id)).json() for w in (first, second)}
    assert answers == {"a", "b"}


async def test_watch_returns_a_newer_revision_or_the_current_one_at_its_timeout(harness: HarnessEnvironment):
    k = key()
    now = (await harness.client.object_call(_signals.notifications, key=k, arg=None))["revision"]
    timed_out = await harness.client.object_call(_signals.watch, key=k, arg=watching(now, 0.2))
    assert timed_out == {"revision": now}
    watcher = asyncio.ensure_future(harness.client.object_call(_signals.watch, key=k, arg=watching(now, 30)))
    await asyncio.sleep(0.3)
    # A wait that starts publishes a change.
    await harness.client.object_send(_signals.wait, key=k, arg=waiting("inv:5", "x"))
    assert (await watcher)["revision"] > now


async def test_a_turn_is_answered_until_it_closes_and_a_failed_one_is_closed_for_it(harness: HarnessEnvironment):
    k = key()
    await harness.client.service_call(start_turn, arg={"key": k, "invocation": "inv_running"})
    status = typing.cast(Status, await harness.client.object_call(_signals.status, key=k, arg=None))
    assert status["answering"] is not None and status["answering"]["invocationId"] == "inv_running"
    await harness.client.service_call(close_turn, arg={"key": k, "invocation": "inv_running"})
    assert (await harness.client.object_call(_signals.status, key=k, arg=None))["answering"] is None

    # A turn whose invocation failed without closing itself: awaitTurn closes it.
    killed = await harness.client.service_send(fail, arg=None)
    await harness.client.service_call(start_turn, arg={"key": k, "invocation": killed.invocation_id})

    async def closed() -> bool:
        return (await harness.client.object_call(_signals.status, key=k, arg=None))["answering"] is None

    await until(closed)
