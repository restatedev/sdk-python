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
"""An agent's companion object, against a real Restate server.

The companion is ingress-private: the tests call it as its agent's handlers do, from a service (``driver``).
"""

# pylint: disable=missing-function-docstring

import asyncio
import json
import typing
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2
import pytest

import restate
from support import RESTATE_IMAGE  # pylint: disable=import-error
from restate import HarnessEnvironment
from restate.agents._api import Published, Signal, Status, Steered, Watch
from restate.agents._signals import STEERS, Started, WaitRequest, make_companion


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

companion = make_companion("assistant")
"""The companion of the agent ``assistant`` (below), ``assistant.prebuilt.restate``."""

# The agent's side: its handlers call the companion's handlers, which the ingress refuses.
driver = restate.Service("driver")


@driver.handler()
async def call(ctx: restate.Context, request: dict[str, Any]) -> Any:
    """Call the companion's ``handler`` at ``key``, with ``arg`` if given, and return what it returns."""
    arg = json.dumps(request["arg"]).encode() if "arg" in request else b""
    answer = await ctx.generic_call(companion.service.name, request["handler"], arg, key=request["key"])
    return json.loads(answer) if answer else None


@driver.handler()
async def send(ctx: restate.Context, request: dict[str, Any]) -> str:
    """Send ``arg`` to the companion's ``handler`` at ``key``, and return the invocation's id."""
    arg = json.dumps(request["arg"]).encode()
    sent = ctx.generic_send(companion.service.name, request["handler"], arg, key=request["key"])
    return await sent.invocation_id()


async def companion_call(harness: HarnessEnvironment, handler: str, k: str, *arg: Any) -> Any:
    request: dict[str, Any] = {"handler": handler, "key": k}
    if arg:
        request["arg"] = arg[0]
    return await harness.client.service_call(call, arg=request)


async def companion_send(harness: HarnessEnvironment, handler: str, k: str, arg: Any) -> str:
    return await harness.client.service_call(send, arg={"handler": handler, "key": k, "arg": arg})


async def start_turn(harness: HarnessEnvironment, k: str, invocation: str, **flags: bool) -> Published | None:
    turn: Started = {"invocationId": invocation, "since": "2026-11-04T00:00:00.000Z"}
    turn.update(typing.cast(Started, flags))
    return await companion_call(harness, "start", k, turn)


async def close_turn(harness: HarnessEnvironment, k: str, invocation: str) -> None:
    await companion_call(harness, "close", k, invocation)


async def status_of(harness: HarnessEnvironment, k: str) -> Status:
    return typing.cast(Status, await companion_call(harness, "status", k))


async def revision_of(harness: HarnessEnvironment, k: str) -> int:
    return (await companion_call(harness, "notifications", k))["revision"]


settled: list[tuple[str, str]] = []
"""The turns the companion had the agent settle: its key, and the turn's invocation."""

placeholders: list[tuple[str, dict[str, str]]] = []
"""The steered messages the companion queued as turns of their own: the agent's key, and the message."""

assistant = restate.VirtualObject("assistant")
"""The companion's agent, keyed as its companion, which records the turns it is sent to settle and the messages it
is sent to answer."""


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
        restate.app([companion.service, driver, failing, assistant]), restate_image=RESTATE_IMAGE
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
    return str(uuid.uuid4())


def waiting(wait_id: str, name: str) -> WaitRequest:
    return {"id": wait_id, "name": name}


def watching(revision: int, timeout: float) -> Watch:
    return {"revision": revision, "timeoutSeconds": timeout}


def signal(**fields: Any) -> Signal:
    return typing.cast(Signal, fields)


async def test_the_companion_is_named_for_its_agent_and_refuses_the_ingress(harness: HarnessEnvironment):
    assert companion.service.name == "assistant.prebuilt.restate"
    async with httpx2.AsyncClient(timeout=60) as admin:
        listed = (await admin.get(f"{harness.admin_api_url}/services")).json()["services"]
    served = {service["name"]: service for service in listed}
    assert served["assistant.prebuilt.restate"]["public"] is False
    assert "signals" not in served
    async with httpx2.AsyncClient(timeout=60) as ingress:
        refused = await ingress.post(f"{harness.ingress_url}/assistant.prebuilt.restate/{key()}/status")
    assert refused.is_client_error


async def test_a_wait_returns_the_value_of_the_signal_that_resolves_it(harness: HarnessEnvironment):
    k = key()
    sent = await companion_send(harness, "wait", k, waiting("inv:1", "approval"))

    async def listed() -> bool:
        return len(await companion_call(harness, "pending", k)) == 1

    await until(listed)
    [pending] = await companion_call(harness, "pending", k)
    assert pending["id"] == "inv:1" and pending["name"] == "approval" and pending["since"].endswith("Z")
    resolved = await companion_call(harness, "resolve", k, signal(id="inv:1", value="approved"))
    assert resolved == {"resolved": "inv:1"}
    answer = await attach(harness, sent)
    assert answer.json() == "approved"
    assert await companion_call(harness, "pending", k) == []


async def test_a_signal_resolves_the_oldest_wait_of_its_name_or_is_dropped_or_kept(harness: HarnessEnvironment):
    k = key()
    # No one waits: dropped, unless kept for the next wait.
    assert await companion_call(harness, "resolve", k, signal(name="go", value=1)) == {"resolved": None}
    await companion_call(harness, "resolve", k, signal(name="go", value=2, keep=True))
    kept = await companion_send(harness, "wait", k, waiting("inv:2", "go"))
    assert (await attach(harness, kept)).json() == 2

    first = await companion_send(harness, "wait", k, waiting("inv:3", "go"))
    second = await companion_send(harness, "wait", k, waiting("inv:4", "go"))

    async def both() -> bool:
        return len(await companion_call(harness, "pending", k)) == 2

    await until(both)
    assert await companion_call(harness, "resolve", k, signal(name="go", value="a")) in (
        {"resolved": "inv:3"},
        {"resolved": "inv:4"},
    )
    await companion_call(harness, "resolve", k, signal(name="go", value="b"))
    answers = {(await attach(harness, w)).json() for w in (first, second)}
    assert answers == {"a", "b"}


async def test_watch_returns_a_newer_revision_or_the_current_one_at_its_timeout(harness: HarnessEnvironment):
    k = key()
    now = await revision_of(harness, k)
    timed_out = await companion_call(harness, "watch", k, watching(now, 0.2))
    assert timed_out == {"revision": now}
    watcher = asyncio.ensure_future(companion_call(harness, "watch", k, watching(now, 30)))
    await asyncio.sleep(0.3)
    # A wait that starts publishes a change.
    await companion_send(harness, "wait", k, waiting("inv:5", "x"))
    assert (await watcher)["revision"] > now


async def test_a_turn_is_answered_until_it_closes_and_a_failed_one_is_closed_for_it(harness: HarnessEnvironment):
    k = key()
    await start_turn(harness, k, "inv_running")
    status = await status_of(harness, k)
    assert status["answering"] is not None and status["answering"]["invocationId"] == "inv_running"
    await close_turn(harness, k, "inv_running")
    assert (await status_of(harness, k))["answering"] is None

    # A turn whose invocation failed without closing itself: awaitTurn closes it.
    killed = await harness.client.service_send(fail, arg=None)
    await start_turn(harness, k, killed.invocation_id)

    async def closed() -> bool:
        return (await status_of(harness, k))["answering"] is None

    await until(closed)


async def test_a_turn_whose_invocation_is_gone_is_settled_by_its_agent_at_the_same_key(harness: HarnessEnvironment):
    k = key()
    gone = await harness.client.service_send(fail, arg=None)
    await start_turn(harness, k, gone.invocation_id)
    # The status shows the turn as the agent's views give it, with no field of the companion's own.
    answering = (await status_of(harness, k))["answering"]
    assert answering is None or set(answering) == {"invocationId", "since"}

    async def closed() -> bool:
        return (await status_of(harness, k))["answering"] is None

    await until(closed)

    async def sent() -> bool:
        return (k, gone.invocation_id) in settled

    await until(sent)


# --- Steered messages -----------------------------------------------------------------------------


async def steer(harness: HarnessEnvironment, k: str, message_id: str, text: str) -> Steered:
    return await companion_call(harness, "steer", k, {"id": message_id, "text": text})


async def published(harness: HarnessEnvironment, k: str) -> Published:
    return await companion_call(harness, "publish", k)


async def taken(harness: HarnessEnvironment, k: str, ids: list[str]) -> None:
    await companion_call(harness, "taken", k, ids)


async def queued(k: str, message_id: str) -> bool:
    return any(at == k and message["id"] == message_id for at, message in placeholders)


async def test_a_steered_message_is_queued_as_a_turn_and_held_for_a_turn_that_steers_until_taken(
    harness: HarnessEnvironment,
):
    k = key()
    await start_turn(harness, k, "inv_steers", steers=True)
    assert await steer(harness, k, "s1", "use Celsius") == {"id": "s1", "turn": "inv_steers"}
    # Queued as a turn of its own at once, in case the turn does not take it.
    await until(lambda: queued(k, "s1"))
    assert [m for _, m in placeholders if m["id"] == "s1"] == [{"id": "s1", "text": "use Celsius"}]
    # The turn learns of it from its next publish, and status shows it.
    assert (await published(harness, k)).get("steering") == [{"id": "s1", "text": "use Celsius"}]
    status = await status_of(harness, k)
    assert [one["id"] for one in status.get("steering", [])] == ["s1"]
    # Once the turn stored it, it is gone.
    await taken(harness, k, ["s1"])
    assert "steering" not in await published(harness, k)
    assert "steering" not in await status_of(harness, k)
    # Sent again, it is still the turn's, and not held again; marked taken again, nothing changes or publishes.
    assert await steer(harness, k, "s1", "use Celsius") == {"id": "s1", "turn": "inv_steers"}
    assert "steering" not in await published(harness, k)
    now = await revision_of(harness, k)
    await taken(harness, k, ["s1"])
    assert await revision_of(harness, k) == now


async def test_a_turn_that_does_not_steer_holds_nothing_and_a_closed_turn_forgets_what_it_left(
    harness: HarnessEnvironment,
):
    k = key()
    # No turn, or one that takes no steered messages: only the turn of its own.
    assert await steer(harness, k, "s1", "hello") == {"id": "s1", "turn": None}
    await start_turn(harness, k, "inv_plain")
    assert await steer(harness, k, "s2", "hello again") == {"id": "s2", "turn": None}
    assert "steering" not in await published(harness, k)
    await until(lambda: queued(k, "s2"))
    await close_turn(harness, k, "inv_plain")

    # A turn that steers ends without taking it: the message is left to its own turn.
    await start_turn(harness, k, "inv_left", steers=True)
    assert (await steer(harness, k, "s3", "too late"))["turn"] == "inv_left"
    await close_turn(harness, k, "inv_left")
    await start_turn(harness, k, "inv_next", steers=True)
    assert "steering" not in await published(harness, k)
    await until(lambda: queued(k, "s3"))

    # The agent's clear forgets them too.
    assert (await steer(harness, k, "s4", "forget me"))["turn"] == "inv_next"
    await companion_call(harness, "reset", k)
    assert "steering" not in await published(harness, k)


async def test_a_turn_takes_at_most_its_share_of_steered_messages(harness: HarnessEnvironment):
    k = key()
    await start_turn(harness, k, "inv_busy", steers=True)
    for n in range(STEERS):
        assert (await steer(harness, k, f"s{n}", f"message {n}"))["turn"] == "inv_busy"
    # Past them, a message is answered as a turn of its own only.
    assert await steer(harness, k, "past", "one more") == {"id": "past", "turn": None}
    assert len((await published(harness, k)).get("steering", [])) == STEERS
    # The same message sent again is held once.
    assert (await steer(harness, k, "s0", "message 0"))["turn"] == "inv_busy"
    assert len((await published(harness, k)).get("steering", [])) == STEERS
    # The messages the turn took still count: it has taken its share, and the next is a turn of its own.
    await taken(harness, k, [f"s{n}" for n in range(STEERS)])
    assert "steering" not in await published(harness, k)
    assert await steer(harness, k, "later", "and more") == {"id": "later", "turn": None}
    assert "steering" not in await published(harness, k)


async def test_a_turn_that_publishes_is_published_as_it_starts_and_gets_what_publish_returns(
    harness: HarnessEnvironment,
):
    k = key()

    # A turn that does not publish (a background agent's): nothing is published.
    before = await revision_of(harness, k)
    assert await start_turn(harness, k, "inv_quiet") is None
    assert await revision_of(harness, k) == before
    await close_turn(harness, k, "inv_quiet")

    # A followed turn: its start is its first publish, with the new revision.
    before = await revision_of(harness, k)
    started = await start_turn(harness, k, "inv_loud", steers=True, publishes=True)
    assert started == {"revision": before + 1}
    # A retried attempt starts it again: it publishes again, with the messages steered to it meanwhile.
    await steer(harness, k, "s1", "use Celsius")
    again = await start_turn(harness, k, "inv_loud", steers=True, publishes=True)
    assert again is not None and again["revision"] == await revision_of(harness, k)
    assert again.get("steering") == [{"id": "s1", "text": "use Celsius"}]
    status = await status_of(harness, k)
    assert status["answering"] is not None and status["answering"]["invocationId"] == "inv_loud"
    await close_turn(harness, k, "inv_loud")
