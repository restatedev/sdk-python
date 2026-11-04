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
"""
Signals to agents: a virtual object per agent, keyed ``<agent>:<key>`` (such as
``assistant:alice``), whose ``wait`` returns once ``resolve`` gives the wait its
value; the message the agent answers; and the revision of the agent's
conversation, which its ``watch`` waits on. As sdk-sessions' ``signals.ts``,
with the same handlers.

An agent's own handler cannot wait on an awakeable (it has no journal to replay
one from), so it calls ``wait``, which can: a journaled handler of this object,
which waits without holding a process.
"""

from __future__ import annotations

import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

import restate
from restate import ObjectContext, ObjectSharedContext, TerminalError
from restate.agents._api import Notifications, Resolved, Signal, Status, Turn, Waiting, Watch
from restate.sessions import SessionContext, ViewContext
from restate.sessions._link import Link
from restate.sessions.internal import definition_of, needing

T = typing.TypeVar("T")
D = typing.TypeVar("D")

signals = restate.VirtualObject("signals")
"""
The signals of every agent, keyed ``<agent>:<key>`` (``signals_of``).
``sessions.app`` serves it beside the agents; its ingress-private handlers are
called by the agents' own handlers.
"""

WATCH = timedelta(hours=1)
"""How long one ``awaitTurn`` waits for its turn to end, before a new one takes over."""


class _WaitRequest(TypedDict):
    id: str
    name: str


class WaitRequest(_WaitRequest, total=False):
    """A tool's wait: its id, the signal it waits for, and what for (the model's input), if anything."""

    input: Any


class _WaitingOn(Waiting):
    """A wait, with its awakeable."""

    awakeable: str


class _Watching(TypedDict):
    """A ``watch`` waiting for a revision newer than ``revision``, by its awakeable."""

    revision: int
    awakeable: str


Context = ObjectContext | ObjectSharedContext


async def _get(ctx: Context, key: str, default: T) -> T:
    """The state ``key``, or ``default`` while it has none."""
    value = await ctx.get(key, type_hint=object)
    return typing.cast(T, value) if value is not None else default


def _public(wait: _WaitingOn) -> Waiting:
    """A wait, as ``pending`` lists it: without its awakeable."""
    shown: Waiting = {"id": wait["id"], "name": wait["name"], "since": wait["since"]}
    if "input" in wait:
        shown["input"] = wait["input"]
    return shown


def iso(seconds: float) -> str:
    """``seconds`` since the epoch as an ISO date, as JavaScript's ``toISOString`` gives it."""
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _publish(ctx: ObjectContext) -> Notifications:
    """Increase the revision, and answer each ``watch`` waiting for it: the new revision."""
    revision = await _get(ctx, "revision", 0) + 1
    ctx.set("revision", revision)
    notified: Notifications = {"revision": revision}
    for watching in await _get(ctx, "watching", typing.cast(list[_Watching], [])):
        ctx.resolve_awakeable(watching["awakeable"], notified)
    ctx.clear("watching")
    return notified


async def _closed(ctx: ObjectContext, turn: str) -> None:
    """
    ``turn`` (an invocation id) is over: forget it, if the agent answers it,
    and its tools' waits, which no one waits for any more, ending their
    ``wait``s (a turn that ends as it should has none left, but a killed one
    leaves them). It publishes the change; a close again changes nothing, and
    publishes nothing.
    """
    answering = await _get(ctx, "turn", typing.cast(Turn | None, None))
    is_answered = answering is not None and answering["invocationId"] == turn
    if is_answered:
        ctx.clear("turn")
    waiting = await _get(ctx, "waiting", typing.cast(list[_WaitingOn], []))
    # A wait's id starts with the invocation id of the turn whose tool waits.
    over = [wait for wait in waiting if wait["id"].startswith(f"{turn}:")]
    for wait in over:
        ctx.reject_awakeable(wait["awakeable"], "its turn is over")
    if over:
        ctx.set("waiting", [wait for wait in waiting if not wait["id"].startswith(f"{turn}:")])
    if is_answered or over:
        await _publish(ctx)


@signals.handler(kind="shared")
async def wait(ctx: ObjectSharedContext, request: WaitRequest) -> Any:
    """Wait for the signal ``name``, as wait ``id`` (for ``input``), and return its value."""
    awakeable, value = ctx.awakeable(type_hint=object)
    since = iso(await ctx.time())
    waiting: _WaitingOn = {"id": request["id"], "name": request["name"], "since": since, "awakeable": awakeable}
    if "input" in request:
        waiting["input"] = request["input"]
    ctx.object_send(listen, ctx.key(), waiting)
    try:
        return await value
    except TerminalError as error:
        # The agent's invocation was cancelled: no one waits any more.
        if error.status_code == 409:
            ctx.object_send(forget, ctx.key(), request["id"])
        raise


@signals.handler()
async def resolve(ctx: ObjectContext, signal: Signal) -> Resolved:
    """Resolve the wait ``id``, or else the oldest wait for ``name``, with ``value``."""
    wait_id, name = signal.get("id"), signal.get("name")
    if wait_id is None and name is None:
        raise TerminalError("a signal needs an id or a name")
    value = signal.get("value")
    waiting = await _get(ctx, "waiting", typing.cast(list[_WaitingOn], []))
    found = next(
        (at for at, wait in enumerate(waiting) if (wait["name"] == name if wait_id is None else wait["id"] == wait_id)),
        None,
    )
    if found is None:
        if signal.get("keep", False) and name is not None and wait_id is None:
            kept = await _get(ctx, "kept", typing.cast(dict[str, list[Any]], {}))
            ctx.set("kept", {**kept, name: [*kept.get(name, []), value]})
        return {"resolved": None}
    resolved = waiting.pop(found)
    ctx.resolve_awakeable(resolved["awakeable"], value)
    ctx.set("waiting", waiting)
    await _publish(ctx)
    return {"resolved": resolved["id"]}


@signals.handler(kind="shared")
async def pending(ctx: ObjectSharedContext) -> list[Waiting]:
    """The waits, oldest first."""
    return [_public(wait) for wait in await _get(ctx, "waiting", typing.cast(list[_WaitingOn], []))]


@signals.handler(ingress_private=True)
async def listen(ctx: ObjectContext, waiting: _WaitingOn) -> None:
    """A wait: resolved now with a signal kept for it, or else when one comes."""
    kept = await _get(ctx, "kept", typing.cast(dict[str, list[Any]], {}))
    values = kept.get(waiting["name"], [])
    if values:
        ctx.resolve_awakeable(waiting["awakeable"], values[0])
        ctx.set("kept", {**kept, waiting["name"]: values[1:]})
        return
    ctx.set("waiting", [*await _get(ctx, "waiting", typing.cast(list[_WaitingOn], [])), waiting])
    await _publish(ctx)


@signals.handler(ingress_private=True)
async def start(ctx: ObjectContext, turn: Turn) -> None:
    """
    The agent's ``chat`` started answering ``turn``: the same invocation again
    (a retry) keeps when it started. A turn before it that never closed itself
    (its invocation was killed) closes now. ``awaitTurn`` watches the new turn,
    so that it closes even if no turn comes after it.
    """
    before = await _get(ctx, "turn", typing.cast(Turn | None, None))
    if before is not None and before["invocationId"] == turn["invocationId"]:
        return
    if before is not None:
        await _closed(ctx, before["invocationId"])
    ctx.set("turn", turn)
    ctx.object_send(await_turn, ctx.key(), turn["invocationId"])


@signals.handler(
    name="awaitTurn",
    kind="shared",
    ingress_private=True,
    # It leaves nothing behind once done: a turn has one at a time.
    journal_retention=timedelta(0),
    idempotency_retention=timedelta(0),
)
async def await_turn(ctx: ObjectSharedContext, turn: str) -> None:
    """
    Wait for ``turn`` to end, without holding a process, and close it if it
    failed: its waits end, and ``status`` no longer says the agent answers it.
    A turn that never closed itself failed (killed, out of retries, lost); one
    that ended as it should, or was cancelled, closed itself already.

    It waits ``WATCH`` at most: a turn still answered then gets a new watch,
    which runs on the deployment of the day. A cancelled watch (an
    operator's) closes nothing.
    """
    attached = ctx.attach_invocation(turn, type_hint=object)
    # This watch's own cancellation raises here, and closes nothing; the turn's failure does not.
    completed, _ = await restate.wait_completed(attached, ctx.sleep(WATCH))
    if attached in completed:
        try:
            await attached
        except TerminalError:
            ctx.object_send(close, ctx.key(), turn)
        return
    answering = await _get(ctx, "turn", typing.cast(Turn | None, None))
    if answering is not None and answering["invocationId"] == turn:
        ctx.object_send(await_turn, ctx.key(), turn)


@signals.handler(kind="shared")
async def status(ctx: ObjectSharedContext) -> Status:
    """What the agent does: the message it answers, if any, and the waits of its tools."""
    return {
        "answering": await _get(ctx, "turn", typing.cast(Turn | None, None)),
        "waiting": [_public(wait) for wait in await _get(ctx, "waiting", typing.cast(list[_WaitingOn], []))],
    }


@signals.handler(kind="shared")
async def watch(ctx: ObjectSharedContext, request: Watch) -> Notifications:
    """
    The revision, once there is one newer than ``revision``, or at the timeout
    (50 s by default, at most 60 s), whichever comes first. It waits in
    Restate, holding no process; a caller that retries it with the same
    idempotency key joins the same wait.
    """
    awakeable, changed = ctx.awakeable(type_hint=object)
    watching: _Watching = {"revision": request["revision"], "awakeable": awakeable}
    ctx.object_send(subscribe, ctx.key(), watching)
    seconds = min(max(float(request.get("timeoutSeconds", 50)), 0.0), 60.0)
    which, value = await restate.select(changed=changed, timeout=ctx.sleep(timedelta(seconds=seconds)))
    if which == "changed":
        return typing.cast(Notifications, value)
    ctx.object_send(unsubscribe, ctx.key(), awakeable)
    return {"revision": await _get(ctx, "revision", 0)}


@signals.handler(ingress_private=True)
async def close(ctx: ObjectContext, turn: str) -> None:
    """The message ``turn`` (an invocation id) is answered, or its turn ended otherwise: see ``_closed``."""
    await _closed(ctx, turn)


@signals.handler(ingress_private=True)
async def reset(ctx: ObjectContext) -> None:
    """The agent forgot its conversation (``clear``): forget its waits and kept signals, and publish the change."""
    ctx.clear("waiting")
    ctx.clear("kept")
    await _publish(ctx)


@signals.handler(ingress_private=True)
async def forget(ctx: ObjectContext, wait_id: str) -> None:
    """Forget wait ``wait_id``, whose agent no longer waits."""
    waiting = await _get(ctx, "waiting", typing.cast(list[_WaitingOn], []))
    ctx.set("waiting", [wait for wait in waiting if wait["id"] != wait_id])
    await _publish(ctx)


@signals.handler(ingress_private=True)
async def publish(ctx: ObjectContext) -> Notifications:
    """The conversation changed: increase its revision, and answer the ``watch``es."""
    return await _publish(ctx)


@signals.handler(kind="shared")
async def notifications(ctx: ObjectSharedContext) -> Notifications:
    """The conversation's revision."""
    return {"revision": await _get(ctx, "revision", 0)}


@signals.handler(ingress_private=True)
async def subscribe(ctx: ObjectContext, watching: _Watching) -> None:
    """Answer ``awakeable`` once there is a revision newer than ``revision``: at once, if there is."""
    revision = await _get(ctx, "revision", 0)
    if revision > watching["revision"]:
        ctx.resolve_awakeable(watching["awakeable"], {"revision": revision})
        return
    ctx.set("watching", [*await _get(ctx, "watching", typing.cast(list[_Watching], [])), watching])


@signals.handler(ingress_private=True)
async def unsubscribe(ctx: ObjectContext, awakeable: str) -> None:
    """Forget the ``watch`` of ``awakeable``, which no longer waits."""
    watching = await _get(ctx, "watching", typing.cast(list[_Watching], []))
    ctx.set("watching", [one for one in watching if one["awakeable"] != awakeable])


# --- An agent's companion -------------------------------------------------------------------------


def key_of(service: str, key: str) -> str:
    """The key of the signals of agent ``service``'s key ``key``: ``<agent>:<key>``."""
    return f"{service}:{key}"


def signals_of(ctx: SessionContext | ViewContext) -> str:
    """The key of the signals of the agent of ``ctx``."""
    definition = definition_of(ctx)
    if definition is None:
        raise TypeError("signals_of takes the context of an agent's handler or view")
    return key_of(definition.name, ctx.key)


@dataclass(frozen=True)
class Companion:
    """
    An agent's companion, for one of its keys: its ``signals`` object, which
    knows what the agent is doing (the message it answers, what its tools wait
    for), and how to reach it.
    """

    key: str
    """The key of the agent's key there: ``<agent>:<key>``."""
    agent: str
    """The agent's name."""
    agent_key: str
    """The agent's key."""


_COMPANION: Link[Callable[[str], Companion]] = Link("restate-agents.companion")
"""How an agent reaches its companion, for one of its keys."""


def with_companion(definition: D) -> D:
    """``definition`` (an agent), pointing to its companion; ``sessions.app`` serves its ``signals`` object beside it."""
    name = typing.cast(str, getattr(definition, "name"))  # noqa: B009 (a definition of the SDK's)
    _COMPANION.on(definition, lambda key: Companion(key=key_of(name, key), agent=name, agent_key=key))
    return needing(definition, lambda: [signals])


def companion_of(agent: object, key: str) -> Companion:
    """The companion of ``agent``'s key ``key``: for anyone holding an agent to reach what it is doing."""
    companion = _COMPANION.of(agent)
    if companion is None:
        raise TypeError(f"{agent!r} is not an agent of restate.agents")
    return companion(key)


def has_companion(agent: object) -> bool:
    """Whether ``agent`` is an object of restate.agents, with a companion."""
    return _COMPANION.of(agent) is not None


def deliver(ctx: SessionContext, agent: object, key: str, text: str, *, idempotency_key: str | None = None) -> None:
    """
    Deliver ``text`` to ``agent``'s key ``key``, from a session handler: a
    message of its own (as ``chat``), which it answers after the messages
    before it. It is sent, not awaited: ``idempotency_key`` makes it
    delivered once::

        deliver(ctx, triage, "ENG-1", "The issue was closed", idempotency_key=delivery)
    """
    companion = companion_of(agent, key)  # an agent of restate.agents, with a `chat` handler
    ctx.generic_send(companion.agent, "chat", text, key=key, idempotency_key=idempotency_key)


__all__: Sequence[str] = ["Companion", "companion_of", "deliver", "signals"]
