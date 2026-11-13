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
"""Session objects set up for agents.

``AgentObject`` adds the views every agent has; its ``chat()`` method turns it
into an agent that answers messages.
"""

from __future__ import annotations

import dataclasses
import functools
import typing
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Any, TypedDict

from restate.agents import _messages, _signals
from restate.agents._api import (
    AgentContext,
    AgentViewContext,
    Messages,
    MessagesRequest,
    Notifications,
    Resolved,
    Signal,
    Status,
    Steer,
    Steered,
    Stopped,
    View,
    Waiting,
    Watch,
)
from restate.agents._common import (
    BUDGET,
    WINDOW,
    CompactHistory,
    MessageSerde,
    Summarize,
    Window,
    check_window,
    refusal_of,
)
from restate.agents._conversation import SCHEMA, Items, next_start, restart, steer_stored
from restate.agents._conversation import compact as compact_turns
from restate.agents._conversation import settle as settle_turn
from restate.agents._turn import AGENT, AgentInfo, chat_turn, cleared, publish, publish_end, publish_steps
from restate.object import VirtualObject
from restate.retry_policy import InvocationRetryPolicy
from restate.sessions import Migration, SessionContext, SessionObject
from restate.sessions.internal import reset

A = typing.TypeVar("A", bound=Callable[[AgentContext, str], Awaitable[Any]])
"""The function that answers a chat message: it takes the message and returns the answer."""

RETRIES = InvocationRetryPolicy(
    initial_interval=timedelta(milliseconds=50),
    exponentiation_factor=2,
    max_interval=timedelta(seconds=10),
    max_attempts=100_000,
)
"""The default retry policy of an agent's handlers.

Each attempt resumes from the database, so an agent can keep retrying for
weeks. The first retry comes soon after a crash, and each wait doubles, up to
10 seconds, so that a provider that rate-limits is not asked again twice a
second.
"""


class AgentOptions(TypedDict, total=False):
    """The options of an agent's object, which every framework's ``agent()`` takes as keyword arguments.

    Attributes:
        description: The object's description, shown in Restate's UI.
        migrations: Tables of your own, beside the agent's.
        background: Whether the agent works unwatched (a subagent, say): it
            then publishes its progress once a turn ends, not at each step.
        metadata: The object's metadata.
        inactivity_timeout: Restate's inactivity timeout for its handlers.
        abort_timeout: Restate's abort timeout for its handlers.
        journal_retention: How long Restate keeps an invocation's journal.
        idempotency_retention: How long Restate keeps the result of an
            invocation that has an idempotency key.
        ingress_private: Whether only other services may call it.
        invocation_retry_policy: How Restate retries a failed turn. Defaults
            to ``RETRIES``.
        window: How many of the latest answered turns the model sees whole,
            with their tool calls and results, or ``"all"``. Defaults to 1.
        budget: How many tokens of the turns before the window the model
            sees, as their messages and answers: the latest that fit.
            Defaults to ``BUDGET``.
    """

    description: str | None
    migrations: Sequence[Migration]
    background: bool
    metadata: dict[str, str] | None
    inactivity_timeout: timedelta | None
    abort_timeout: timedelta | None
    journal_retention: timedelta | None
    idempotency_retention: timedelta | None
    ingress_private: bool | None
    invocation_retry_policy: InvocationRetryPolicy | None
    window: Window
    budget: int


WAITS = timedelta(seconds=15)
"""The inactivity timeout of the views that wait (``watch``, ``sync`` and ``clear``).

Restate suspends a view that waits longer than this, which frees its compute
slot. The call it waits for resumes it: it runs again from the start, and
joins the same calls through their idempotency keys.
"""


class SyncRequest(TypedDict, total=False):
    """The request of the ``sync`` view.

    ``sync`` returns the messages that are new or changed since ``cursor``
    (default 0, which means the whole conversation), at most ``limit`` of them
    (default 100). If ``revision`` is given, it first waits until the revision
    is newer than that, or until ``timeoutSeconds`` pass (default 50, at most
    60).
    """

    cursor: int
    revision: int
    timeoutSeconds: float
    limit: int
    view: View
    """How much of each turn the messages show: ``"full"`` (the default) or ``"summary"``."""


class Synced(Messages):
    """The response of the ``sync`` view: a page of messages, as ``messages`` returns it, plus the status and the revision."""

    revision: int
    status: Status


Nothing: typing.TypeAlias = None
"""The input of the views that take none: an optional body, which they ignore.

A handler with no input refuses a request with a body or a content type,
and some clients send one with every call: Restate's TypeScript client, for
one, sends ``content-type: application/json`` even without an argument. An
optional input takes those requests and requests without a body alike.
"""


BUILT_IN = frozenset(
    {
        "signal",
        "pending",
        "status",
        "stop",
        "notifications",
        "watch",
        "chat",
        "messages",
        "clear",
        "sync",
        "erase",
        "settle",
        "compact",
        "steer",
        "steered",
    }
)
"""The names of the built-in handlers and views of every agent, which are reserved."""


class AgentObject(SessionObject):
    """A session object set up for agents.

    Like any session object, it is a Restate virtual object with a SQL
    database per key (``ctx.db``), and it can have handlers and views of your
    own. Every agent also has these built-in views:

    - ``status``: what the agent is doing: the message it is answering, if
      any, and the signals its tools are waiting for.
    - ``stop``: cancel the message being answered.
    - ``pending``: list the signals its tools are waiting for, oldest first.
    - ``signal``: resolve one of those waits with a ``value``, by its ``id``
      or by signal ``name``.
    - ``notifications``: return the conversation's revision, which increases
      with each change.
    - ``watch``: wait for a revision newer than a given one.

    ``chat()`` turns the object into an agent, and adds:

    - ``chat``: a handler that takes a message and returns the answer.
    - ``steer``: send a message while a turn is answered: the turn takes it
      at its next step, or else it is answered as a turn of its own, in the
      order it was sent.
    - ``messages``: read the conversation, one page at a time.
    - ``clear``: stop the message being answered and forget the
      conversation, emptying the key's database.
    - ``sync``: read what changed since a cursor, together with the status
      and the revision, for clients that follow the conversation.

    These names, ``erase`` (which ``clear`` uses), ``settle`` (which closes
    a turn whose invocation is gone), ``compact`` (which summarizes the
    earliest turns) and ``steered`` (which answers a steered message that no
    turn took), are reserved:
    registering a handler or view of your own under one of them raises
    ``ValueError``. You rarely create an ``AgentObject`` yourself: a
    framework's ``agent()`` creates one and calls ``chat()`` for you.

    Args:
        name: The object's name, as Restate serves it.
        description: The object's description, shown in Restate's UI.
        migrations: Tables of your own, beside the agent's (whose names
            start with ``restate_``).
        background: Whether the agent works unwatched (a subagent, for
            example). A background agent publishes its progress only when a
            turn ends, not after each step.
        metadata: The object's metadata.
        inactivity_timeout: Restate's inactivity timeout for its handlers.
        abort_timeout: Restate's abort timeout for its handlers.
        journal_retention: How long Restate keeps an invocation's journal.
        idempotency_retention: How long Restate keeps the result of an
            invocation that has an idempotency key.
        ingress_private: Whether only other services may call it.
        invocation_retry_policy: How Restate retries a failed handler.
            Defaults to ``RETRIES``.
        window: How many of the latest answered turns the model sees whole,
            with their tool calls and results, or ``"all"``: earlier turns
            are their messages and answers. Defaults to 1.
        budget: How many tokens of the turns before the window the model
            sees: the latest that fit. Defaults to ``BUDGET``.
    """

    def __init__(
        self,
        name: str,
        *,
        description: str | None = None,
        migrations: Sequence[Migration] = (),
        background: bool = False,
        metadata: dict[str, str] | None = None,
        inactivity_timeout: timedelta | None = None,
        abort_timeout: timedelta | None = None,
        journal_retention: timedelta | None = None,
        idempotency_retention: timedelta | None = None,
        ingress_private: bool | None = None,
        invocation_retry_policy: InvocationRetryPolicy | None = RETRIES,
        window: Window = WINDOW,
        budget: int = BUDGET,
    ) -> None:
        super().__init__(
            name,
            migrations=migrations,
            description=description,
            metadata=metadata,
            inactivity_timeout=inactivity_timeout,
            abort_timeout=abort_timeout,
            journal_retention=journal_retention,
            idempotency_retention=idempotency_retention,
            ingress_private=ingress_private,
            invocation_retry_policy=invocation_retry_policy,
        )
        self.background: bool = background
        self._clear: Callable[[SessionContext], Awaitable[None]] | None = None
        given = typing.cast(object, budget)
        if isinstance(given, bool) or not isinstance(given, int) or given < 0:
            raise ValueError(f"budget must be a number of tokens (0 or more), not {budget!r}")
        self._settings = AgentInfo(background=background, window=check_window(window), budget=budget)
        AGENT.on(self, self._settings)
        self._companion = _signals.with_companion(self)
        self._building = True
        try:
            self._built_in()
        finally:
            self._building = False

    def __restate_services__(self) -> tuple[VirtualObject, ...]:
        """What ``restate.app`` serves for an agent (see ``restate.Servable``): itself, and its own companion,
        ``<agent>.prebuilt.restate``, which its tools wait on."""
        return (self, self._companion.service)

    def _reserved(self, name: str) -> str | None:
        if name not in BUILT_IN or getattr(self, "_building", False):
            return None
        return (
            f"{name!r} is a built-in handler of every agent ({', '.join(sorted(BUILT_IN))}): "
            f"give yours another name, such as @{self.name}.handler(name='my_{name}')"
        )

    def _attempt_fn(self, fn: Callable[..., Awaitable[Any]], view: bool) -> Callable[..., Awaitable[Any]]:
        """Wrap a handler so that a model provider's refusal fails its invocation for good, as in ``chat``, the
        steps its turns store publish (``publish_steps``), as in ``chat``, and each turn's answer publishes once
        more (``publish_end``): for a handler of your own that prompts.

        A ``chat`` turn is wrapped the same way, and then keeps its end for
        its ``close`` (see ``chat_turn``): it publishes no end of its own."""
        if view:
            return fn

        # Keep fn's signature: the SDK reads from it whether the handler takes an input.
        @functools.wraps(fn)
        async def answering(ctx: AgentContext, *args: Any) -> Any:
            publish_steps(ctx)
            publish_end(ctx)
            try:
                return await fn(ctx, *args)
            except Exception as error:
                terminal = refusal_of(ctx, error)
                if terminal is None:
                    raise
                raise terminal from error

        return answering

    def _built_in(self) -> None:
        """Register the views every agent has; each one asks the agent's companion."""
        companion = self._companion

        async def signal(ctx: AgentViewContext, signal: Signal) -> Resolved:
            """Resolve a wait of the agent's tools with ``value``: the wait with ``id``, or the oldest one for ``name``."""
            return await ctx.call(companion.resolve, signal, key=ctx.key, idempotency_key=f"{ctx.request.id}:signal")

        async def pending(ctx: AgentViewContext, _: Nothing = None) -> list[Waiting]:
            """List the signals the agent's tools are waiting for, oldest first."""
            return await ctx.call(companion.pending, key=ctx.key)

        async def status(ctx: AgentViewContext, _: Nothing = None) -> Status:
            """Return what the agent is doing: the message it is answering, if any, and what its tools wait for."""
            return await ctx.call(companion.status, key=ctx.key)

        async def stop(ctx: AgentViewContext, _: Nothing = None) -> Stopped:
            """Cancel the invocation that is answering the current message, if there is one."""
            return await _stopped(ctx, companion, "stop")

        async def notifications(ctx: AgentViewContext, _: Nothing = None) -> Notifications:
            """Return the conversation's revision, which increases whenever ``messages`` or ``status`` change."""
            return await ctx.call(companion.notifications, key=ctx.key)

        async def watch(ctx: AgentViewContext, request: Watch) -> Notifications:
            """Wait for a revision newer than ``revision``, then return the current one.

            It returns anyway after ``timeoutSeconds`` (50 by default, at most
            60). The wait happens in Restate and holds no process.
            """
            return await ctx.call(companion.watch, request, key=ctx.key, idempotency_key=f"{ctx.request.id}:watch")

        for fn in (signal, pending, status, stop, notifications):
            self.view(fn)
        # A client follows with one watch (or sync) after another: their journals are not kept.
        self.view(inactivity_timeout=WAITS, journal_retention=timedelta(0))(watch)

    @typing.overload
    def chat(
        self,
        answer: A,
        /,
        *,
        items: Items,
        clear: Callable[[SessionContext], Awaitable[None]] | None = None,
        output: type[Any] | None = None,
        summarize: Summarize | None = None,
        snapshots: bool = False,
        compact_history: CompactHistory | None = None,
        steers: bool = False,
    ) -> A: ...

    @typing.overload
    def chat(
        self,
        /,
        *,
        items: Items,
        clear: Callable[[SessionContext], Awaitable[None]] | None = None,
        output: type[Any] | None = None,
        summarize: Summarize | None = None,
        snapshots: bool = False,
        compact_history: CompactHistory | None = None,
        steers: bool = False,
    ) -> Callable[[A], A]: ...

    def chat(
        self,
        answer: Callable[[AgentContext, str], Awaitable[Any]] | None = None,
        /,
        *,
        items: Items,
        clear: Callable[[SessionContext], Awaitable[None]] | None = None,
        output: type[Any] | None = None,
        summarize: Summarize | None = None,
        snapshots: bool = False,
        compact_history: CompactHistory | None = None,
        steers: bool = False,
    ) -> Any:
        """Turn this object into an agent that answers each message with ``answer``.

        Call it directly or use it as a decorator. Besides the ``chat``
        handler, it adds the ``messages``, ``clear`` and ``sync`` views.

        The message is the request's body, as plain text or as a JSON
        string::

            curl localhost:8080/assistant/alice/chat -d 'Hi'

        Each key answers one message at a time. A message that the model's
        provider refuses (a missing API key, an unknown model) fails with
        status 424 instead of being retried.

        Args:
            answer: A framework's turn: it takes the handler's context and
                the message, and returns the answer. It should keep the
                conversation with ``answer_once``, and stop when
                ``stop_signal_of(ctx)`` stops.
            items: The framework's items, with which the ``messages`` view
                reads the conversation.
            clear: Clears whatever the agent keeps outside its database; the
                ``clear`` view calls it after emptying the database.
            output: The type of ``answer``'s answers, if not text (see the
                frameworks' ``output``). It is the ``chat`` handler's output,
                as Restate's discovery and serde see it.
            summarize: Summarizes the turns before the window once they
                outgrow the agent's ``budget``, from the request (``SUMMARIZE``
                and the turns, as text) to the summary, which then stands for
                them. It runs in an invocation of its own (the private
                ``compact`` handler), after the answer. Without it, the
                earliest turns are only left out.
            snapshots: Whether the agent takes its history from snapshots
                that its framework's own compaction stores
                (``Turn.snapshot``): each turn gives the model the latest
                snapshot, then every item stored after it, whole. The
                agent's ``window`` and ``budget``, and ``summarize``, then do
                not apply.
            compact_history: With ``snapshots``, compacts the history between
                turns, from the history to the compacted history (or
                ``None``), which is stored as a snapshot. It runs in the
                private ``compact`` handler, after a turn that asked for it
                (``Conversation.compact_after``).
            steers: Whether ``answer`` takes steered messages at the turn's
                steps (``take_steers``), which a framework's integration
                says. Without, every message sent to ``steer`` is answered as
                a turn of its own, after the turn being answered.

        Returns:
            ``answer`` itself.
        """
        if answer is None:

            def decorate(
                fn: Callable[[AgentContext, str], Awaitable[Any]],
            ) -> Callable[[AgentContext, str], Awaitable[Any]]:
                return self.chat(
                    fn,
                    items=items,
                    clear=clear,
                    output=output,
                    summarize=summarize,
                    snapshots=snapshots,
                    compact_history=compact_history,
                    steers=steers,
                )

            return decorate
        if compact_history is not None and not snapshots:
            raise ValueError("compact_history compacts snapshots: pass snapshots=True with it")
        self._settings = dataclasses.replace(
            self._settings,
            background=self.background,
            clear=clear,
            summarize=summarize,
            snapshots=snapshots,
            compact_history=compact_history,
            steers=steers,
        )
        AGENT.on(self, self._settings)
        answering = answer

        async def chat(ctx: AgentContext, text: str) -> Any:
            return await chat_turn(ctx, text, answering)

        chat.__annotations__["return"] = output if output is not None else str

        async def steered(ctx: AgentContext, message: Steer) -> None:
            """Answer a message sent to ``steer`` as a turn of its own, unless the turn it was sent to took it.

            The companion sends it as the message is sent, so that it keeps its
            place in the key's queue: it runs once the turns before it ended.
            """
            if await steer_stored(ctx.db, message["id"]):
                return
            await chat_turn(ctx, message["text"], answering)

        self._building = True
        try:
            self.handler(name="chat", accept="*/*", input_serde=MessageSerde(), explicit_cancellation=True)(chat)
            self.handler(name="steered", ingress_private=True, explicit_cancellation=True)(steered)
            _followed(self, items)
        finally:
            self._building = False
        return answer


async def _stopped(ctx: AgentViewContext, companion: _signals.Companion, step: str) -> Stopped:
    """Cancel the invocation answering the current message, if any, and return its id.

    The status is read with an idempotency key, made of the request id and
    ``step``, so a restarted view cancels the same invocation again (a no-op)
    rather than the next message's. The companion's other calls from a view
    that change something (``signal``, ``steer``) or wait (``watch``) have
    such a key too, so a restarted view joins the same call; reads have none.
    """
    status = await ctx.call(companion.status, key=ctx.key, idempotency_key=f"{ctx.request.id}:{step}")
    turn = status["answering"]
    if turn is not None:
        ctx.cancel(turn["invocationId"])
    return {"stopped": turn["invocationId"] if turn is not None else None}


def _followed(agent: AgentObject, items: Items) -> None:
    """Register the views that ``chat()`` adds (``messages``, ``steer``, ``clear``, ``sync``), and the private
    handlers."""
    companion = _signals.companion_of(agent)

    async def messages(ctx: AgentViewContext, request: MessagesRequest | None = None) -> Messages:
        """Read the conversation: a page of it, or what changed since a cursor.

        Send ``{"limit": n, "before": id}`` for a page, or ``{"cursor": c}``
        for the messages that are new or changed since ``c``. While a message
        is being answered, it returns what has been stored so far.
        """
        return await _messages.page(ctx.db, items, request)

    async def steer(ctx: AgentViewContext, text: str) -> Steered:
        """Send a message to the agent while it answers another: the turn takes it at its next step.

        The message is the request's body, as plain text or as a JSON string,
        as for ``chat``. The turn being answered gives it to the model at its
        next step (once the model's tool calls have their results), if its
        framework takes steered messages and it reaches one; otherwise the
        message is answered as a turn of its own, after the turns before it,
        in the order it was sent. With no turn being answered, it starts one.
        Either way it is answered once: send it with an idempotency key, so
        that a retried request is the same message.
        """
        message: Steer = {"id": ctx.request.id, "text": text}
        return await ctx.call(companion.steer, message, key=ctx.key, idempotency_key=f"{ctx.request.id}:steer")

    async def erase(ctx: AgentContext) -> None:
        """Empty the key's database and its signals, once the handlers queued before it are done."""
        # Read first, which also makes the reset apply the conversation's schema again.
        await SCHEMA.ensure(ctx.db)
        start = await next_start(ctx.db)
        async with ctx.db.transaction() as tx:
            await reset(tx)
            await restart(tx, start)
        await cleared(ctx)
        await ctx.call(companion.reset, key=ctx.key, idempotency_key=f"{ctx.request.id}:reset")

    async def settle(ctx: AgentContext, invocation_id: str) -> bool:
        """Close the latest turn, unanswered, if invocation ``invocation_id`` ran it and is gone.

        The companion sends this once it finds the turn's invocation ended
        without answering (killed, say), so that the conversation shows the
        turn closed before the next message. Return whether it closed it.
        """
        if not await settle_turn(ctx, items, invocation_id):
            return False
        # The companion published as it closed the turn, before this stored how it ended: publish that too.
        await publish(ctx)
        return True

    async def compact(ctx: AgentContext, _: Nothing = None) -> bool:
        """Compact the history: summarize the turns past the budget, or compact a snapshot. Return whether it did."""
        return await compact_turns(ctx, items)

    async def clear(ctx: AgentViewContext, _: Nothing = None) -> Stopped:
        """Forget the conversation, and empty the key's database.

        It stops the message being answered, as ``stop`` does. Messages sent
        before the ``clear`` are still answered first, then forgotten too.
        Message ids and cursors carry on from where they were, so a client
        that follows the conversation keeps reading, and drops the messages
        before ``first``.
        """
        stopped = await _stopped(ctx, companion, "clear")
        await ctx.call(erase, key=ctx.key, idempotency_key=f"{ctx.request.id}:erase")
        return stopped

    async def sync(ctx: AgentViewContext, request: SyncRequest | None = None) -> Synced:
        """Return the conversation since ``cursor``, what the agent is doing, and the revision.

        With ``revision``, it first waits for a newer revision, or for the
        timeout. A client follows the conversation by sending back the
        ``revision`` and ``cursor`` of each response, right away while
        ``more`` is true.
        """
        request = request or {}
        revision = request.get("revision")
        if revision is not None:
            watch: Watch = {"revision": revision, "timeoutSeconds": request.get("timeoutSeconds", 50)}
            await ctx.call(companion.watch, watch, key=ctx.key, idempotency_key=f"{ctx.request.id}:watch")
        # Read the revision first, then the page in an invocation of its own, which starts after it: a view
        # reads the database as committed when it started, and this one started before the revision (by
        # the whole wait, when it waited). So the page has every change the revision counts.
        now = await ctx.call(companion.notifications, key=ctx.key)
        status = await ctx.call(companion.status, key=ctx.key)
        wanted: MessagesRequest = {"cursor": request.get("cursor", 0), "limit": request.get("limit", 100)}
        if "view" in request:
            wanted["view"] = request["view"]
        page = await ctx.call(messages, wanted, key=ctx.key)
        synced: Synced = {**page, "revision": now["revision"], "status": status}
        return synced

    agent.view(messages)
    agent.view(accept="*/*", input_serde=MessageSerde())(steer)
    agent.handler(ingress_private=True)(erase)
    agent.handler(ingress_private=True)(settle)
    agent.handler(ingress_private=True)(compact)
    agent.view(inactivity_timeout=WAITS)(clear)
    agent.view(inactivity_timeout=WAITS, journal_retention=timedelta(0))(sync)
