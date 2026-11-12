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
"""Each agent's companion: a virtual object of its own, ``<agent>.agentprebuilt.restate``.

Whatever makes an agent (a framework's ``agent()``, ``AgentObject``) makes its
companion too (``with_companion``), and the agent serves it alongside itself.
The companion of agent ``assistant`` is ``assistant.agentprebuilt.restate``,
keyed as the agent: key ``alice`` of the agent has key ``alice`` of its
companion. It holds the signals the agent's tools wait for (``wait`` returns
once ``resolve`` gives the wait its value), the message the agent is
answering, the messages steered to it that it has not taken yet (``steer``),
and the conversation's revision, which ``watch`` waits on.

The companion is ingress-private: only its agent's handlers and views call it,
and clients reach it through the agent's views (``status``, ``pending``,
``signal``, ``steer``, ``watch``, ...). Its handlers' names, the camelCase
``awaitTurn`` among them, and payloads are those of the TypeScript SDK's
companion.

An agent's own handler cannot wait on an awakeable, since it has no journal to
replay one from. Instead it calls ``wait``, a regular journaled handler of
its companion, which waits without holding a process.
"""

from __future__ import annotations

import json
import typing
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

import restate
from restate import ObjectContext, ObjectSharedContext, TerminalError
from restate.agents._api import (
    Answering,
    Notifications,
    Published,
    Resolved,
    Signal,
    Status,
    Steer,
    Steered,
    Steering,
    Waiting,
    Watch,
)
from restate.sessions import SessionContext, ViewContext
from restate.sessions._link import Link
from restate.sessions.internal import definition_of

T = typing.TypeVar("T")

COMPANION_SUFFIX = ".agentprebuilt.restate"
"""What an agent's name is followed by to name its companion: ``<agent>.agentprebuilt.restate``.

Restate refuses service names that start with ``restate``, so the companion's
name ends with it instead."""


def companion_name(agent: str) -> str:
    """Return the name of the companion of agent ``agent``: ``<agent>.agentprebuilt.restate``."""
    return f"{agent}{COMPANION_SUFFIX}"


WATCH = timedelta(hours=1)
"""How long one ``awaitTurn`` invocation waits for its turn to end before handing over to a new one."""

PARKED = timedelta(seconds=1)
"""The inactivity timeout of the handlers that wait (``wait``, ``awaitTurn`` and ``watch``).

They do nothing but wait, so Restate suspends them after a second, which frees
their compute slot; the completion they wait for resumes them, replaying their
short journals.
"""

STEERS = 16
"""How many messages a turn takes by ``steer``, at most: past them, each is answered as a turn of its own.

The companion counts each message it holds for the turn, taken or not, until
the turn ends: so a turn that someone steers on and on stays within bounds,
and so does what the companion holds for it."""


class _WaitRequest(TypedDict):
    id: str
    name: str


class WaitRequest(_WaitRequest, total=False):
    """A tool's wait: its id, the signal it waits for, and optionally the model's input describing what for."""

    input: Any


class _WaitingOn(Waiting):
    """A wait, together with its awakeable."""

    awakeable: str


class _Watching(TypedDict):
    """A ``watch`` waiting for a revision newer than ``revision``, identified by its awakeable."""

    revision: int
    awakeable: str


Context = ObjectContext | ObjectSharedContext


class Started(Answering, total=False):
    """A turn as its agent starts it: and whether it takes steered messages, and whether ``start`` publishes it."""

    steers: bool
    """Whether the turn takes steered messages at its steps (``steer``). A turn that does not (of an agent whose
    framework cannot take them, or that runs in the background) gets none: each is answered as a turn of its own."""
    publishes: bool
    """Whether ``start`` publishes the turn's start, and returns what ``publish`` returns: a followed turn, which
    then makes no publish of its own. A turn that does not (of an agent that runs in the background) is not
    published by ``start``, which returns nothing."""


class _Held(TypedDict):
    """A message steered to the turn ``turn`` (an invocation id), and when it was sent."""

    id: str
    since: str
    turn: str


class _Inboxed(_Held, total=False):
    """A message steered to a turn, as the companion holds it until the turn ends: with its text until the turn
    takes it (``taken``), and then only as one of the turn's ``STEERS``."""

    text: str


def _shown(turn: Started | None) -> Answering | None:
    """Return ``turn`` as ``status`` shows it: without what only the companion reads."""
    return None if turn is None else {"invocationId": turn["invocationId"], "since": turn["since"]}


async def _inbox(ctx: Context) -> list[_Inboxed]:
    """Return the messages steered to the turns not closed yet (the one being answered), taken or not, oldest
    first."""
    return await _get(ctx, "inbox", typing.cast(list[_Inboxed], []))


def _waiting(inbox: list[_Inboxed], answering: Started | None) -> list[_Inboxed]:
    """Return the messages of ``inbox`` steered to the turn being answered (``answering``) that it has not taken yet,
    oldest first: those with their text."""
    if answering is None:
        return []
    return [one for one in inbox if one["turn"] == answering["invocationId"] and "text" in one]


async def _get(ctx: Context, key: str, default: T) -> T:
    """Return the state ``key``, or ``default`` if it is unset."""
    value = await ctx.get(key, type_hint=object)
    return typing.cast(T, value) if value is not None else default


def _public(wait: _WaitingOn) -> Waiting:
    """Return a wait as ``pending`` lists it, without its awakeable."""
    shown: Waiting = {"id": wait["id"], "name": wait["name"], "since": wait["since"]}
    if "input" in wait:
        shown["input"] = wait["input"]
    return shown


def iso(seconds: float) -> str:
    """Format ``seconds`` since the epoch as an ISO 8601 UTC timestamp with milliseconds, like JavaScript's ``toISOString``."""
    moment = datetime.fromtimestamp(seconds, tz=UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def _publish(ctx: ObjectContext) -> Notifications:
    """Increase the revision, wake up every waiting ``watch``, and return the new revision."""
    revision = await _get(ctx, "revision", 0) + 1
    ctx.set("revision", revision)
    notified: Notifications = {"revision": revision}
    watching = await _get(ctx, "watching", typing.cast(list[_Watching], []))
    for one in watching:
        ctx.resolve_awakeable(one["awakeable"], notified)
    if watching:
        ctx.clear("watching")
    return notified


async def _closed(ctx: ObjectContext, agent: str, turn: str, gone: bool = False) -> None:
    """Close ``turn`` (an invocation id): forget it and its tools' waits, and publish the change.

    The waits are rejected, which ends their ``wait`` calls. A turn that ends
    normally has no waits left, but a killed one can leave some behind.
    Closing a turn twice changes nothing and publishes nothing.

    If the turn's invocation is ``gone`` and the turn did not close itself, the
    agent (``agent``, whose companion this is) is sent ``settle``, which closes
    the turn in its conversation too.

    The messages steered to the turn are forgotten here: each that it did not
    take was queued as a turn of its own as it was sent (``steer``), and is
    answered as one.
    """
    answering = await _get(ctx, "turn", typing.cast(Started | None, None))
    is_answered = answering is not None and answering["invocationId"] == turn
    if is_answered:
        ctx.clear("turn")
        if gone:
            ctx.generic_send(agent, "settle", json.dumps(turn).encode(), key=ctx.key())
    inbox = await _inbox(ctx)
    dropped = any(one["turn"] == turn for one in inbox)
    if dropped:
        _set_inbox(ctx, [one for one in inbox if one["turn"] != turn])
    waiting = await _get(ctx, "waiting", typing.cast(list[_WaitingOn], []))
    # A wait's id starts with the invocation id of the turn whose tool waits.
    over = [wait for wait in waiting if wait["id"].startswith(f"{turn}:")]
    for wait in over:
        ctx.reject_awakeable(wait["awakeable"], "its turn is over")
    if over:
        ctx.set("waiting", [wait for wait in waiting if not wait["id"].startswith(f"{turn}:")])
    if is_answered or over or dropped:
        await _publish(ctx)


def _set_inbox(ctx: ObjectContext, inbox: list[_Inboxed]) -> None:
    if inbox:
        ctx.set("inbox", inbox)
    else:
        ctx.clear("inbox")


async def _published(ctx: ObjectContext) -> Published:
    """Publish (``_publish``), and return the new revision and the messages the turn being answered has not taken."""
    published: Published = {**await _publish(ctx)}
    inbox = await _inbox(ctx)
    if any("text" in one for one in inbox):
        # Only then is the turn read: a turn that nobody steers costs one read more, of the empty inbox.
        answering = await _get(ctx, "turn", typing.cast(Started | None, None))
        steering: list[Steer] = [{"id": one["id"], "text": one.get("text", "")} for one in _waiting(inbox, answering)]
        if steering:
            published["steering"] = steering
    return published


@dataclass(frozen=True)
class Companion:
    """An agent's companion object (``service``), and its handlers, to call with the agent's key.

    From a handler or view of the agent::

        status = await ctx.call(companion_of(assistant).status, key=ctx.key)
    """

    agent: str
    """The name of the agent whose companion this is."""
    service: restate.VirtualObject
    """The companion's virtual object, ``<agent>.agentprebuilt.restate``, which the agent serves alongside itself."""
    steer: Callable[[ObjectContext, Steer], Awaitable[Steered]]
    taken: Callable[[ObjectContext, list[str]], Awaitable[None]]
    wait: Callable[[ObjectSharedContext, WaitRequest], Awaitable[Any]]
    resolve: Callable[[ObjectContext, Signal], Awaitable[Resolved]]
    pending: Callable[[ObjectSharedContext], Awaitable[list[Waiting]]]
    listen: Callable[[ObjectContext, _WaitingOn], Awaitable[None]]
    start: Callable[[ObjectContext, Started], Awaitable[Published | None]]
    await_turn: Callable[[ObjectSharedContext, str], Awaitable[None]]
    status: Callable[[ObjectSharedContext], Awaitable[Status]]
    watch: Callable[[ObjectSharedContext, Watch], Awaitable[Notifications]]
    close: Callable[[ObjectContext, str], Awaitable[None]]
    ended: Callable[[ObjectContext, str], Awaitable[None]]
    reset: Callable[[ObjectContext], Awaitable[None]]
    forget: Callable[[ObjectContext, str], Awaitable[None]]
    publish: Callable[[ObjectContext], Awaitable[Published]]
    notifications: Callable[[ObjectSharedContext], Awaitable[Notifications]]
    subscribe: Callable[[ObjectContext, _Watching], Awaitable[None]]
    unsubscribe: Callable[[ObjectContext, str], Awaitable[None]]


def make_companion(agent: str) -> Companion:
    """Make the companion of agent ``agent``: the virtual object ``<agent>.agentprebuilt.restate``, keyed as the agent.

    It knows its agent: it sends it ``settle`` and ``steered``. It is
    ingress-private: only the agent's handlers and views call it.
    """
    service = restate.VirtualObject(
        companion_name(agent),
        description=f"The companion of agent {agent}: its turn, its tools' waits, steered messages and revision",
        ingress_private=True,
    )

    @service.handler()
    async def steer(ctx: ObjectContext, message: Steer) -> Steered:
        """Take a message steered to the agent (its ``steer`` view): for the turn being answered, or as a turn of its own.

        The message is always sent to the agent's private ``steered`` handler at
        once, which keeps its place in the key's queue, in order with the
        messages sent after it: once the turn being answered ends, ``steered``
        answers the message as a turn of its own, unless that turn took it. A
        turn that takes steered messages (``Started.steers``) also finds it here,
        at its next step, through ``publish``, until it takes it (``taken``) or
        ends.
        """
        message_id, text = message["id"], message["text"]
        placeholder = json.dumps({"id": message_id, "text": text}).encode()
        ctx.generic_send(agent, "steered", placeholder, key=ctx.key(), idempotency_key=f"steer:{message_id}")
        answering = await _get(ctx, "turn", typing.cast(Started | None, None))
        if answering is None or not answering.get("steers", False):
            return {"id": message_id, "turn": None}
        turn, inbox = answering["invocationId"], await _inbox(ctx)
        if any(one["id"] == message_id for one in inbox):
            return {"id": message_id, "turn": turn}
        # Taken or not: the turn has room for so many in all.
        if sum(one["turn"] == turn for one in inbox) >= STEERS:
            return {"id": message_id, "turn": None}
        since = iso(await ctx.time())
        _set_inbox(ctx, [*inbox, {"id": message_id, "text": text, "since": since, "turn": turn}])
        await _publish(ctx)
        return {"id": message_id, "turn": turn}

    @service.handler()
    async def taken(ctx: ObjectContext, ids: list[str]) -> None:
        """Mark the steered messages ``ids`` taken, which their turn has stored, and publish the change, if any.

        The turn sends it with the transaction that stores them, so it comes once
        they are stored, and once only. Each is kept without its text until the
        turn ends, as one of its ``STEERS``; one already forgotten (its turn
        closed first) is left so.
        """
        inbox = await _inbox(ctx)
        marked: list[_Inboxed] = [
            {"id": one["id"], "since": one["since"], "turn": one["turn"]} if one["id"] in ids else one for one in inbox
        ]
        if marked != inbox:
            _set_inbox(ctx, marked)
            await _publish(ctx)

    @service.handler(kind="shared", inactivity_timeout=PARKED)
    async def wait(ctx: ObjectSharedContext, request: WaitRequest) -> Any:
        """Wait for the signal ``name`` as wait ``id``, and return its value."""
        awakeable, value = ctx.awakeable(type_hint=object)
        since = iso(await ctx.time())
        waiting: _WaitingOn = {"id": request["id"], "name": request["name"], "since": since, "awakeable": awakeable}
        if "input" in request:
            waiting["input"] = request["input"]
        ctx.object_send(listen, ctx.key(), waiting)
        try:
            return await value
        except TerminalError as error:
            # The agent's invocation was cancelled, so nothing waits any more.
            if error.status_code == 409:
                ctx.object_send(forget, ctx.key(), request["id"])
            raise

    @service.handler()
    async def resolve(ctx: ObjectContext, signal: Signal) -> Resolved:
        """Resolve the wait ``id``, or else the oldest wait for signal ``name``, with ``value``."""
        wait_id, name = signal.get("id"), signal.get("name")
        if wait_id is None and name is None:
            raise TerminalError("a signal needs an id or a name")
        value = signal.get("value")
        waiting = await _get(ctx, "waiting", typing.cast(list[_WaitingOn], []))
        found = next(
            (
                at
                for at, wait in enumerate(waiting)
                if (wait["name"] == name if wait_id is None else wait["id"] == wait_id)
            ),
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

    @service.handler(kind="shared")
    async def pending(ctx: ObjectSharedContext) -> list[Waiting]:
        """List the waits, oldest first."""
        return [_public(wait) for wait in await _get(ctx, "waiting", typing.cast(list[_WaitingOn], []))]

    @service.handler()
    async def listen(ctx: ObjectContext, waiting: _WaitingOn) -> None:
        """Register a wait, resolving it right away if a signal was kept for it."""
        kept = await _get(ctx, "kept", typing.cast(dict[str, list[Any]], {}))
        values = kept.get(waiting["name"], [])
        if values:
            ctx.resolve_awakeable(waiting["awakeable"], values[0])
            ctx.set("kept", {**kept, waiting["name"]: values[1:]})
            return
        ctx.set("waiting", [*await _get(ctx, "waiting", typing.cast(list[_WaitingOn], [])), waiting])
        await _publish(ctx)

    @service.handler()
    async def start(ctx: ObjectContext, turn: Started) -> Published | None:
        """Record that the agent's ``chat`` started answering ``turn``, and publish it if the turn ``publishes``.

        A retry of the same invocation keeps the original start time. A previous
        turn that never closed itself (because its invocation was killed) is
        closed now. ``awaitTurn`` then watches the new turn, so that it is closed
        even if no other turn follows it.

        A turn that ``publishes`` gets what ``publish`` returns (the turn's first
        publish, made here rather than by a call of its own); any other gets
        ``None``, and publishes itself if it is followed.
        """
        before = await _get(ctx, "turn", typing.cast(Started | None, None))
        if before is None or before["invocationId"] != turn["invocationId"]:
            if before is not None:
                await _closed(ctx, agent, before["invocationId"], gone=True)
            ctx.set("turn", turn)
            ctx.object_send(await_turn, ctx.key(), turn["invocationId"])
        if not turn.get("publishes", False):
            return None
        return await _published(ctx)

    @service.handler(
        name="awaitTurn",
        kind="shared",
        inactivity_timeout=PARKED,
        # Keep nothing once it is done: there is one at a time per turn.
        journal_retention=timedelta(0),
        idempotency_retention=timedelta(0),
    )
    async def await_turn(ctx: ObjectSharedContext, turn: str) -> None:
        """Wait for ``turn`` to end, without holding a process, and close it if it failed.

        Closing ends the turn's waits, and ``status`` stops reporting it. A turn
        that ended normally or was cancelled has closed itself already; one that
        did not (killed, out of retries, lost) is closed here.

        It waits at most ``WATCH``: if the turn is still running then, a new
        watch takes over, which runs on the current deployment. Cancelling the
        watch itself (an operator might) closes nothing.
        """
        attached = ctx.attach_invocation(turn, type_hint=object)
        # Cancelling this watch raises here, and closes nothing; the turn failing does not raise here.
        completed, _ = await restate.wait_completed(attached, ctx.sleep(WATCH))
        if attached in completed:
            try:
                await attached
            except TerminalError:
                ctx.object_send(ended, ctx.key(), turn)
            return
        answering = await _get(ctx, "turn", typing.cast(Answering | None, None))
        if answering is not None and answering["invocationId"] == turn:
            ctx.object_send(await_turn, ctx.key(), turn)

    @service.handler(kind="shared")
    async def status(ctx: ObjectSharedContext) -> Status:
        """Return what the agent is doing: the message it is answering, if any, its tools' waits, and the messages
        steered to it that it has not taken yet."""
        answering = await _get(ctx, "turn", typing.cast(Started | None, None))
        shown: Status = {
            "answering": _shown(answering),
            "waiting": [_public(wait) for wait in await _get(ctx, "waiting", typing.cast(list[_WaitingOn], []))],
        }
        waiting = _waiting(await _inbox(ctx), answering)
        steering: list[Steering] = [{"id": one["id"], "since": one["since"]} for one in waiting]
        if steering:
            shown["steering"] = steering
        return shown

    # Its journal is not kept once it is done: a client follows with one watch after another.
    @service.handler(kind="shared", inactivity_timeout=PARKED, journal_retention=timedelta(0))
    async def watch(ctx: ObjectSharedContext, request: Watch) -> Notifications:
        """Wait for a revision newer than ``revision``, or the timeout, and return the current revision.

        The timeout is ``timeoutSeconds`` (50 by default, at most 60). The wait
        happens in Restate and holds no process; a caller that retries with the
        same idempotency key joins the same wait.
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

    @service.handler()
    async def close(ctx: ObjectContext, turn: str) -> None:
        """Close ``turn`` (an invocation id), which has been answered or ended otherwise."""
        await _closed(ctx, agent, turn)

    @service.handler()
    async def ended(ctx: ObjectContext, turn: str) -> None:
        """Close ``turn`` (an invocation id), which failed: if it did not close itself, the agent closes it too."""
        await _closed(ctx, agent, turn, gone=True)

    @service.handler()
    async def reset(ctx: ObjectContext) -> None:
        """Forget all waits, kept signals and steered messages after the agent's ``clear``, and publish the change.
        Each steered message was queued as a turn of its own as it was sent, before the ``clear``."""
        ctx.clear("waiting")
        ctx.clear("kept")
        ctx.clear("inbox")
        await _publish(ctx)

    @service.handler()
    async def forget(ctx: ObjectContext, wait_id: str) -> None:
        """Forget wait ``wait_id``, which the agent no longer waits for."""
        waiting = await _get(ctx, "waiting", typing.cast(list[_WaitingOn], []))
        ctx.set("waiting", [wait for wait in waiting if wait["id"] != wait_id])
        await _publish(ctx)

    @service.handler()
    async def publish(ctx: ObjectContext) -> Published:
        """Increase the conversation's revision after a change, and wake up every waiting ``watch``.

        It returns the new revision, and the messages steered to the turn being
        answered that it has not taken yet, if any: a turn publishes as it starts
        and after each step it stores that a tool waits for (a reply with tool
        calls, the tools' results), so it learns of them at no cost of its own.
        """
        return await _published(ctx)

    @service.handler(kind="shared")
    async def notifications(ctx: ObjectSharedContext) -> Notifications:
        """Return the conversation's revision."""
        return {"revision": await _get(ctx, "revision", 0)}

    @service.handler()
    async def subscribe(ctx: ObjectContext, watching: _Watching) -> None:
        """Resolve the watch's awakeable once there is a revision newer than its ``revision``, right away if there is one."""
        revision = await _get(ctx, "revision", 0)
        if revision > watching["revision"]:
            ctx.resolve_awakeable(watching["awakeable"], {"revision": revision})
            return
        ctx.set("watching", [*await _get(ctx, "watching", typing.cast(list[_Watching], [])), watching])

    @service.handler()
    async def unsubscribe(ctx: ObjectContext, awakeable: str) -> None:
        """Forget the ``watch`` with ``awakeable``, which no longer waits."""
        watching = await _get(ctx, "watching", typing.cast(list[_Watching], []))
        ctx.set("watching", [one for one in watching if one["awakeable"] != awakeable])

    return Companion(
        agent=agent,
        service=service,
        steer=steer,
        taken=taken,
        wait=wait,
        resolve=resolve,
        pending=pending,
        listen=listen,
        start=start,
        await_turn=await_turn,
        status=status,
        watch=watch,
        close=close,
        ended=ended,
        reset=reset,
        forget=forget,
        publish=publish,
        notifications=notifications,
        subscribe=subscribe,
        unsubscribe=unsubscribe,
    )


# --- An agent's companion -------------------------------------------------------------------------


_COMPANION: Link[Companion] = Link("restate-agents.companion")
"""The companion of each agent."""


def with_companion(definition: object) -> Companion:
    """Make the companion of ``definition`` (an agent), attach it, and return it.

    The agent serves it alongside itself (``AgentObject.__restate_services__``).
    """
    name = typing.cast(str, getattr(definition, "name"))  # noqa: B009 (a definition of the SDK's)
    companion = make_companion(name)
    _COMPANION.on(definition, companion)
    return companion


def companion_of(agent: object) -> Companion:
    """Return the companion of ``agent``, whose handlers take the agent's keys, to find out what that agent is doing.

    Raises:
        TypeError: If ``agent`` is not a ``restate.agents`` agent.
    """
    companion = _COMPANION.of(agent)
    if companion is None:
        raise TypeError(f"{agent!r} is not an agent of restate.agents")
    return companion


def companion_at(ctx: SessionContext | ViewContext) -> Companion | None:
    """Return the companion of the agent whose handler or view ``ctx`` runs, or ``None`` for another session
    object's: call it with ``key=ctx.key``."""
    definition = definition_of(ctx)
    return _COMPANION.of(definition) if definition is not None else None


def companion_in(ctx: SessionContext | ViewContext) -> Companion:
    """Return the companion of the agent whose handler or view ``ctx`` runs: call it with ``key=ctx.key``.

    Raises:
        TypeError: If ``ctx`` is not the context of an agent's handler or view.
    """
    companion = companion_at(ctx)
    if companion is None:
        raise TypeError("companion_in takes the context of an agent's handler or view")
    return companion


def needs_agent(ctx: SessionContext, what: str) -> TerminalError:
    """Return the error for ``what`` (a step that only an agent takes, as it needs the companion), from a handler
    of a session object that is no agent: terminal, as a retry would meet it again."""
    definition = definition_of(ctx)
    name = typing.cast(str, getattr(definition, "name", "?"))
    return TerminalError(
        f"{what} needs an agent, whose companion holds its waits: {name!r} is a plain session object. "
        "Use `<framework>.agent(...)` (such as `restate.agents.pydantic_ai.agent`) or `AgentObject` instead of "
        "`SessionObject`"
    )


def waits_in(ctx: SessionContext, what: str) -> Companion:
    """Return the companion of the agent whose handler ``ctx`` runs, to wait for a signal (``what``) on it.

    Raises:
        TerminalError: If ``ctx`` is a handler's of a session object that is no agent (``needs_agent``).
    """
    companion = companion_at(ctx)
    if companion is None:
        raise needs_agent(ctx, what)
    return companion


def has_companion(agent: object) -> bool:
    """Return whether ``agent`` is a ``restate.agents`` agent, with a companion."""
    return _COMPANION.of(agent) is not None


def deliver(
    ctx: SessionContext | restate.Context, agent: object, key: str, text: str, *, idempotency_key: str | None = None
) -> None:
    """Send ``text`` to ``agent``'s key ``key`` as a chat message, from a handler.

    The handler is a session object's, or any Restate handler's (a webhook's,
    say). The agent answers the message after any messages queued before it.
    The message is sent, not awaited; pass ``idempotency_key`` so that a
    retried attempt delivers it only once::

        deliver(ctx, triage, "ENG-1", "The issue was closed", idempotency_key=f"{ctx.request.id}:closed")

    Raises:
        TypeError: If ``agent`` is not a ``restate.agents`` agent.
    """
    agent_name = companion_of(agent).agent  # an agent of restate.agents, with a `chat` handler
    if isinstance(ctx, restate.Context):
        # A Restate handler's context sends bytes: the message as a JSON string, which `chat` takes.
        message = json.dumps(text).encode()
        ctx.generic_send(agent_name, "chat", message, key=key, idempotency_key=idempotency_key)
    else:
        ctx.generic_send(agent_name, "chat", text, key=key, idempotency_key=idempotency_key)


__all__: Sequence[str] = [
    "COMPANION_SUFFIX",
    "Companion",
    "companion_in",
    "companion_name",
    "companion_of",
    "deliver",
    "make_companion",
]
