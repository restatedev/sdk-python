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

import functools
import typing
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Any, TypedDict

from restate.agents import _log, _signals
from restate.agents._api import (
    Messages,
    MessagesRequest,
    Notifications,
    Resolved,
    Signal,
    Status,
    Stopped,
    Waiting,
    Watch,
)
from restate.agents._common import MessageSerde, refusal_of
from restate.agents._turn import AGENT, AgentInfo, chat_turn, cleared
from restate.retry_policy import InvocationRetryPolicy
from restate.sessions import Migration, SessionContext, SessionObject, ViewContext
from restate.sessions.internal import reset

T = typing.TypeVar("T")
A = typing.TypeVar("A", bound=Callable[[SessionContext, str], Awaitable[Any]])
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

WAITS = timedelta(minutes=5)
"""The inactivity timeout of the views that wait (``watch``, ``sync`` and ``clear``).

Restate suspends a view that waits longer than this, which restarts it, so the
timeout is longer than any of their waits.
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


class Synced(Messages):
    """The response of the ``sync`` view: a page of messages, as ``messages`` returns it, plus the status and the revision."""

    revision: int
    status: Status


def _start_after(page: Messages) -> _log.Start:
    """Return where the log restarts after the latest message of ``page``, past its ids and cursor."""
    latest = page["messages"][0] if page["messages"] else None
    return _log.Start(seq=page["cursor"], id=latest["id"] + 1 if latest else page.get("first", 0))


BUILT_IN = frozenset(
    {"signal", "pending", "status", "stop", "notifications", "watch", "chat", "messages", "clear", "sync", "erase"}
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
    - ``messages``: read the conversation, one page at a time.
    - ``clear``: stop the message being answered and forget the
      conversation, emptying the key's database.
    - ``sync``: read what changed since a cursor, together with the status
      and the revision, for clients that follow the conversation.

    These names, and ``erase`` (which ``clear`` uses), are reserved:
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
        AGENT.on(self, AgentInfo(background=background))
        _signals.with_companion(self)
        self._building = True
        try:
            self._built_in()
        finally:
            self._building = False

    def _reserved(self, name: str) -> str | None:
        if name not in BUILT_IN or getattr(self, "_building", False):
            return None
        return (
            f"{name!r} is a built-in handler of every agent ({', '.join(sorted(BUILT_IN))}): "
            f"give yours another name, such as @{self.name}.handler(name='my_{name}')"
        )

    def _attempt_fn(self, fn: Callable[..., Awaitable[Any]], view: bool) -> Callable[..., Awaitable[Any]]:
        """Wrap a handler so that a model provider's refusal fails its invocation for good, as in ``chat``."""
        if view:
            return fn

        # Keep fn's signature: the SDK reads from it whether the handler takes an input.
        @functools.wraps(fn)
        async def answering(ctx: SessionContext, *args: Any) -> Any:
            try:
                return await fn(ctx, *args)
            except Exception as error:
                terminal = refusal_of(ctx, error)
                if terminal is None:
                    raise
                raise terminal from error

        return answering

    def _built_in(self) -> None:
        """Register the views every agent has; each one asks the agent's companion ``signals`` object."""

        async def signal(ctx: ViewContext, signal: Signal) -> Resolved:
            """Resolve a wait of the agent's tools with ``value``: the wait with ``id``, or the oldest one for ``name``."""
            return await _companion(ctx, _signals.resolve, "signal")(signal)

        async def pending(ctx: ViewContext) -> list[Waiting]:
            """List the signals the agent's tools are waiting for, oldest first."""
            return await _companion(ctx, _signals.pending)()

        async def status(ctx: ViewContext) -> Status:
            """Return what the agent is doing: the message it is answering, if any, and what its tools wait for."""
            return await _companion(ctx, _signals.status)()

        async def stop(ctx: ViewContext) -> Stopped:
            """Cancel the invocation that is answering the current message, if there is one."""
            return await _stopped(ctx, "stop")

        async def notifications(ctx: ViewContext) -> Notifications:
            """Return the conversation's revision, which increases whenever ``messages`` or ``status`` change."""
            return await _companion(ctx, _signals.notifications)()

        async def watch(ctx: ViewContext, request: Watch) -> Notifications:
            """Wait for a revision newer than ``revision``, then return the current one.

            It returns anyway after ``timeoutSeconds`` (50 by default, at most
            60). The wait happens in Restate and holds no process.
            """
            return await _companion(ctx, _signals.watch, "watch")(request)

        for fn in (signal, pending, status, stop, notifications):
            self.view(fn)
        self.view(inactivity_timeout=WAITS)(watch)

    @typing.overload
    def chat(
        self,
        answer: A,
        /,
        *,
        clear: Callable[[SessionContext], Awaitable[None]] | None = None,
    ) -> A: ...

    @typing.overload
    def chat(
        self,
        /,
        *,
        clear: Callable[[SessionContext], Awaitable[None]] | None = None,
    ) -> Callable[[A], A]: ...

    def chat(
        self,
        answer: Callable[[SessionContext, str], Awaitable[Any]] | None = None,
        /,
        *,
        clear: Callable[[SessionContext], Awaitable[None]] | None = None,
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
                conversation in the log with ``transcribing``, and stop when
                ``stop_signal_of(ctx)`` stops.
            clear: Clears whatever the agent keeps outside its database; the
                ``clear`` view calls it after emptying the database.

        Returns:
            ``answer`` itself.
        """
        if answer is None:

            def decorate(
                fn: Callable[[SessionContext, str], Awaitable[Any]],
            ) -> Callable[[SessionContext, str], Awaitable[Any]]:
                return self.chat(fn, clear=clear)

            return decorate
        AGENT.on(self, AgentInfo(background=self.background, clear=clear))
        answering = answer

        async def chat(ctx: SessionContext, text: str) -> Any:
            return await chat_turn(ctx, text, answering)

        self._building = True
        try:
            self.handler(name="chat", accept="*/*", input_serde=MessageSerde(), explicit_cancellation=True)(chat)
            _followed(self)
        finally:
            self._building = False
        return answer


def _companion(
    ctx: ViewContext, handler: Callable[..., Awaitable[T]], step: str | None = None
) -> Callable[..., Awaitable[T]]:
    """Return a function that calls ``handler`` on the companion of the agent of ``ctx``.

    With ``step``, the call gets an idempotency key made of the request id and
    ``step``, so a restarted view joins the same call. Reads have no key.
    """
    key = None if step is None else f"{ctx.request.id}:{step}"
    companion = _signals.signals_of(ctx)
    untyped = typing.cast(Any, ctx)

    def call(*args: Any) -> Awaitable[T]:
        return typing.cast(Awaitable[T], untyped.call(handler, *args, key=companion, idempotency_key=key))

    return call


async def _stopped(ctx: ViewContext, step: str) -> Stopped:
    """Cancel the invocation answering the current message, if any, and return its id.

    The status is read with an idempotency key, so a restarted view cancels the
    same invocation again (a no-op) rather than the next message's.
    """
    turn = (await _companion(ctx, _signals.status, step)())["answering"]
    if turn is not None:
        ctx.cancel(turn["invocationId"])
    return {"stopped": turn["invocationId"] if turn is not None else None}


def _followed(agent: AgentObject) -> None:
    """Register the views that ``chat()`` adds (``messages``, ``clear``, ``sync``), and the private ``erase``."""

    async def messages(ctx: ViewContext, request: MessagesRequest | None = None) -> Messages:
        """Read the conversation: a page of it, or what changed since a cursor.

        Send ``{"limit": n, "before": id}`` for a page, or ``{"cursor": c}``
        for the messages that are new or changed since ``c``. While a message
        is being answered, it returns what has been stored so far.
        """
        return await _log.page(ctx.db, request)

    async def erase(ctx: SessionContext) -> None:
        """Empty the key's database and its signals, once the handlers queued before it are done."""
        start = _start_after(await _log.page(ctx.db, {"limit": 1}))
        async with ctx.db.transaction() as tx:
            await reset(tx)
            await _log.restart(tx, start)
        await cleared(ctx)
        await ctx.call(_signals.reset, key=_signals.signals_of(ctx), idempotency_key=f"{ctx.request.id}:reset")

    async def clear(ctx: ViewContext) -> Stopped:
        """Forget the conversation, and empty the key's database.

        It stops the message being answered, as ``stop`` does. Messages sent
        before the ``clear`` are still answered first, then forgotten too.
        Message ids and cursors carry on from where they were, so a client
        that follows the conversation keeps reading, and drops the messages
        before ``first``.
        """
        stopped = await _stopped(ctx, "clear")
        await ctx.call(erase, key=ctx.key, idempotency_key=f"{ctx.request.id}:erase")
        return stopped

    async def sync(ctx: ViewContext, request: SyncRequest | None = None) -> Synced:
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
            await _companion(ctx, _signals.watch, "watch")(watch)
        # Read the revision first, then the page in an invocation of its own, which starts after it: a view
        # reads the database as committed when it started, and this one started before the revision (by
        # the whole wait, when it waited). So the page has every change the revision counts.
        now = await _companion(ctx, _signals.notifications)()
        status = await _companion(ctx, _signals.status)()
        wanted: MessagesRequest = {"cursor": request.get("cursor", 0), "limit": request.get("limit", 100)}
        page = await ctx.call(messages, wanted, key=ctx.key)
        synced: Synced = {**page, "revision": now["revision"], "status": status}
        return synced

    agent.view(messages)
    agent.handler(ingress_private=True)(erase)
    agent.view(inactivity_timeout=WAITS)(clear)
    agent.view(inactivity_timeout=WAITS)(sync)
