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
from support import RESTATE_IMAGE  # pylint: disable=import-error
from restate import HarnessEnvironment
from restate.agents import _signals
from restate.agents._api import Published, Signal, Status, Steered, Watch
from restate.agents._signals import STEERS, Started, WaitRequest


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

# The agent's side: its handlers call the companion's internal handlers, which the ingress refuses.
driver = restate.Service("driver")


@driver.handler()
async def start_turn(ctx: restate.Context, request: dict[str, str]) -> None:
    turn: Started = {"invocationId": request["invocation"], "since": "2026-11-04T00:00:00.000Z"}
    if request.get("settles"):
        turn["settles"] = True
    if request.get("steers"):
        turn["steers"] = True
    await ctx.object_call(_signals.start, key=request["key"], arg=turn)


@driver.handler()
async def close_turn(ctx: restate.Context, request: dict[str, str]) -> None:
    await ctx.object_call(_signals.close, key=request["key"], arg=request["invocation"])


@driver.handler()
async def steer_to(ctx: restate.Context, request: dict[str, str]) -> Steered:
    message = {"id": request["id"], "text": request["text"]}
    return await ctx.object_call(_signals.steer, key=request["key"], arg=typing.cast(_signals.SteerRequest, message))


@driver.handler()
async def publish_to(ctx: restate.Context, key: str) -> Published:
    return await ctx.object_call(_signals.publish, key=key, arg=None)


@driver.handler()
async def taken_by(ctx: restate.Context, request: dict[str, typing.Any]) -> None:
    await ctx.object_call(_signals.taken, key=request["key"], arg=request["ids"])


@driver.handler()
async def reset_of(ctx: restate.Context, key: str) -> None:
    await ctx.object_call(_signals.reset, key=key, arg=None)


settled: list[tuple[str, str]] = []
"""The turns the companion had the agent settle: its key, and the turn's invocation."""

placeholders: list[tuple[str, dict[str, str]]] = []
"""The steered messages the companion queued as turns of their own: the agent's key, and the message."""

assistant = restate.VirtualObject("assistant")
"""The agent of the companions' keys (``assistant:<key>``), which records the turns it is sent to settle."""


@assistant.handler()
async def settle(ctx: restate.ObjectContext, invocation_id: str) -> bool:
    settled.append((ctx.key(), invocation_id))
    return True


@assistant.handler()
async def steered(ctx: restate.ObjectContext, message: dict[str, str]) -> None:
    placeholders.append((ctx.key(), message))


failing = restate.Service("failing")


@failing.handler()
async def fail(ctx: restate.Context) -> None:
    raise restate.TerminalError("this turn was killed")


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        restate.app([_signals.signals, driver, failing, assistant]), restate_image=RESTATE_IMAGE
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


async def test_a_turn_whose_invocation_is_gone_is_settled_by_its_agent_if_the_agent_settles(
    harness: HarnessEnvironment,
):
    k = key()
    agent_key = k.split(":", 1)[1]

    async def closed() -> bool:
        return (await harness.client.object_call(_signals.status, key=k, arg=None))["answering"] is None

    # An agent of another SDK does not say it settles its turns: it is sent nothing.
    other = await harness.client.service_send(fail, arg=None)
    await harness.client.service_call(start_turn, arg={"key": k, "invocation": other.invocation_id})
    await until(closed)
    # The status shows the turn as every SDK's companion does, with no field of this one's own.
    mine = await harness.client.service_send(fail, arg=None)
    await harness.client.service_call(start_turn, arg={"key": k, "invocation": mine.invocation_id, "settles": "yes"})
    answering = (await harness.client.object_call(_signals.status, key=k, arg=None))["answering"]
    assert answering is None or set(answering) == {"invocationId", "since"}
    await until(closed)

    async def sent() -> bool:
        return (agent_key, mine.invocation_id) in settled

    await until(sent)
    assert (agent_key, other.invocation_id) not in settled


# --- Steered messages -----------------------------------------------------------------------------


async def steer(harness: HarnessEnvironment, k: str, message_id: str, text: str) -> Steered:
    return await harness.client.service_call(steer_to, arg={"key": k, "id": message_id, "text": text})


async def published(harness: HarnessEnvironment, k: str) -> Published:
    return await harness.client.service_call(publish_to, arg=k)


async def queued(k: str, message_id: str) -> bool:
    agent_key = k.split(":", 1)[1]
    return any(at == agent_key and message["id"] == message_id for at, message in placeholders)


async def test_a_steered_message_is_queued_as_a_turn_and_held_for_a_turn_that_steers_until_taken(
    harness: HarnessEnvironment,
):
    k = key()
    await harness.client.service_call(start_turn, arg={"key": k, "invocation": "inv_steers", "steers": "yes"})
    assert await steer(harness, k, "s1", "use Celsius") == {"id": "s1", "turn": "inv_steers"}
    # Queued as a turn of its own at once, in case the turn does not take it.
    await until(lambda: queued(k, "s1"))
    assert [m for _, m in placeholders if m["id"] == "s1"] == [{"id": "s1", "text": "use Celsius"}]
    # The turn learns of it from its next publish, and status shows it.
    assert (await published(harness, k)).get("steering") == [{"id": "s1", "text": "use Celsius"}]
    status = typing.cast(Status, await harness.client.object_call(_signals.status, key=k, arg=None))
    assert [one["id"] for one in status.get("steering", [])] == ["s1"]
    # Once the turn stored it, it is gone.
    await harness.client.service_call(taken_by, arg={"key": k, "ids": ["s1"]})
    assert "steering" not in await published(harness, k)
    assert "steering" not in await harness.client.object_call(_signals.status, key=k, arg=None)


async def test_a_turn_that_does_not_steer_holds_nothing_and_a_closed_turn_forgets_what_it_left(
    harness: HarnessEnvironment,
):
    k = key()
    # No turn, or one that takes no steered messages: only the turn of its own.
    assert await steer(harness, k, "s1", "hello") == {"id": "s1", "turn": None}
    await harness.client.service_call(start_turn, arg={"key": k, "invocation": "inv_plain"})
    assert await steer(harness, k, "s2", "hello again") == {"id": "s2", "turn": None}
    assert "steering" not in await published(harness, k)
    await until(lambda: queued(k, "s2"))
    await harness.client.service_call(close_turn, arg={"key": k, "invocation": "inv_plain"})

    # A turn that steers ends without taking it: the message is left to its own turn.
    await harness.client.service_call(start_turn, arg={"key": k, "invocation": "inv_left", "steers": "yes"})
    assert (await steer(harness, k, "s3", "too late"))["turn"] == "inv_left"
    await harness.client.service_call(close_turn, arg={"key": k, "invocation": "inv_left"})
    await harness.client.service_call(start_turn, arg={"key": k, "invocation": "inv_next", "steers": "yes"})
    assert "steering" not in await published(harness, k)
    await until(lambda: queued(k, "s3"))

    # The agent's clear forgets them too.
    assert (await steer(harness, k, "s4", "forget me"))["turn"] == "inv_next"
    await harness.client.service_call(reset_of, arg=k)
    assert "steering" not in await published(harness, k)


async def test_a_turn_takes_at_most_its_share_of_steered_messages(harness: HarnessEnvironment):
    k = key()
    await harness.client.service_call(start_turn, arg={"key": k, "invocation": "inv_busy", "steers": "yes"})
    for n in range(STEERS):
        assert (await steer(harness, k, f"s{n}", f"message {n}"))["turn"] == "inv_busy"
    # Past them, a message is answered as a turn of its own only.
    assert await steer(harness, k, "past", "one more") == {"id": "past", "turn": None}
    assert len((await published(harness, k)).get("steering", [])) == STEERS
    # The same message sent again is held once.
    assert (await steer(harness, k, "s0", "message 0"))["turn"] == "inv_busy"
    assert len((await published(harness, k)).get("steering", [])) == STEERS
