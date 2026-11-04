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
Session objects set up for agents: ``AgentObject``, whose ``chat`` makes it an
agent. As sdk-sessions' ``sessionObject`` of ``@restatedev/agents``
(``object.ts``).
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
from restate.sessions import SessionContext, SessionObject, Step, ViewContext
from restate.sessions.internal import reset

T = typing.TypeVar("T")

RETRIES = InvocationRetryPolicy(
    initial_interval=timedelta(milliseconds=50),
    exponentiation_factor=2,
    max_interval=timedelta(seconds=10),
    max_attempts=100_000,
)
"""
An agent's retries, for weeks, since each attempt resumes from the database:
soon after a crash, then twice as long each time, up to 10 seconds apart (a
provider's rate limit, say, is not asked again twice a second).
"""

WAITS = timedelta(minutes=5)
"""
The inactivity timeout of the views that wait (``watch``, ``sync``, ``clear``):
Restate suspends one that waits past it, which restarts it, so it is longer
than the wait.
"""


class SyncRequest(TypedDict, total=False):
    """
    What ``sync`` reads: what is new or changed since ``cursor`` (default 0,
    all of it), at most ``limit`` messages (default 100); once the revision is
    past ``revision``, if given, or at ``timeoutSeconds`` (default 50, at most
    60).
    """

    cursor: int
    revision: int
    timeoutSeconds: float
    limit: int


class Synced(Messages):
    """What ``sync`` answers: the messages since the cursor, as ``messages`` gives them, the status and the revision."""

    revision: int
    status: Status


def _start_after(page: Messages) -> _log.Start:
    """Where the log starts after the latest message of ``page``: past its ids and cursor."""
    latest = page["messages"][0] if page["messages"] else None
    return _log.Start(seq=page["cursor"], id=latest["id"] + 1 if latest else page.get("first", 0))


class AgentObject(SessionObject):
    """
    A Restate virtual object with a SQL database per key, ``ctx.db``, set up
    for agents. Besides its own handlers and views, it has the views
    ``status``, what it is doing (the message it answers, and what its tools
    wait for), ``stop``, which stops that message, ``pending``, the waits of
    its tools, ``signal({id, value})``, which resolves one, and
    ``notifications`` and ``watch({revision})``, to follow its conversation.

    ``chat(answer)`` makes it an agent: a ``chat`` handler, a message in and
    the answer out, the ``messages`` view (its conversation), ``clear``, which
    forgets it (its database too), and ``sync``, which a client following it
    reads. A framework's ``agent()`` makes one.

    ``schema``: tables of your own, beside the agent's (named ``restate_...``).
    ``background``: it works unwatched (a subagent, say): its turn publishes at
    its end only, not at each step.
    """

    def __init__(
        self,
        name: str,
        *,
        description: str | None = None,
        schema: Sequence[Step] = (),
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
            schema=schema,
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
        self._built_in()

    def _attempt_fn(self, fn: Callable[..., Awaitable[Any]], view: bool) -> Callable[..., Awaitable[Any]]:
        """A handler's attempt: a model provider's refusal it fails with ends its invocation, as ``chat``'s does."""
        if view:
            return fn

        # With fn's signature, from which the SDK takes whether it has an input.
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
        """The views every agent has: each asks the companion, which knows what the object does."""

        async def signal(ctx: ViewContext, signal: Signal) -> Resolved:
            """Resolve a wait of the object's tools: by its ``id``, or the oldest of a ``name``, with ``value``."""
            return await _companion(ctx, _signals.resolve, "signal")(signal)

        async def pending(ctx: ViewContext) -> list[Waiting]:
            """The waits of the object's tools, oldest first."""
            return await _companion(ctx, _signals.pending)()

        async def status(ctx: ViewContext) -> Status:
            """What the object is doing: the message it answers, if any, and what its tools wait for."""
            return await _companion(ctx, _signals.status)()

        async def stop(ctx: ViewContext) -> Stopped:
            """Cancel the invocation of the message being answered (``status``'s), as Restate's admin API cancels one."""
            return await _stopped(ctx, "stop")

        async def notifications(ctx: ViewContext) -> Notifications:
            """The revision of the conversation: each change of what ``messages`` and ``status`` give increases it."""
            return await _companion(ctx, _signals.notifications)()

        async def watch(ctx: ViewContext, request: Watch) -> Notifications:
            """
            The revision, once there is one newer than ``revision``, or at the
            timeout (50 s by default, at most 60 s): a client then reads what
            changed. It waits in Restate, holding no process.
            """
            return await _companion(ctx, _signals.watch, "watch")(request)

        for fn in (signal, pending, status, stop, notifications):
            self.view(fn)
        self.view(inactivity_timeout=WAITS)(watch)

    def chat(
        self,
        answer: Callable[[SessionContext, str], Awaitable[T]],
        *,
        clear: Callable[[SessionContext], Awaitable[None]] | None = None,
    ) -> None:
        """
        Make this object an agent, which answers each message with ``answer``:
        a framework's turn, which keeps the conversation in the log
        (``transcribing``) and stops with ``stop_signal_of(ctx)``. ``clear``,
        what the agent keeps beside its database, which ``clear`` empties too.

        A message comes as the request's body, text or a JSON string::

            curl localhost:8080/assistant/alice/chat -d 'Hi'

        A key answers one message at a time; one its model's provider refused
        (a missing API key, an unknown model) answers with an error (424).
        """
        AGENT.on(self, AgentInfo(background=self.background, clear=clear))

        async def chat(ctx: SessionContext, text: str) -> T:
            return await chat_turn(ctx, text, answer)

        self.handler(name="chat", accept="*/*", input_serde=MessageSerde(), explicit_cancellation=True)(chat)
        _followed(self)


def _companion(
    ctx: ViewContext, handler: Callable[..., Awaitable[T]], step: str | None = None
) -> Callable[..., Awaitable[T]]:
    """
    A call of the companion's of ``ctx``; with ``step``, a change, keyed by the
    request and ``step``: a restarted view joins the same call. A read has no
    key: asked again, it answers again.
    """
    key = None if step is None else f"{ctx.request.id}:{step}"
    return typing.cast(Callable[..., Awaitable[T]], ctx.rpc(handler, key=_signals.signals_of(ctx), idempotency_key=key))


async def _stopped(ctx: ViewContext, step: str) -> Stopped:
    """
    Cancel the invocation of the message being answered, if any, and say
    which. The companion's ``status`` says which, with a key of the request and
    ``step``: a restarted view cancels the same invocation again (which does
    nothing), not the message that started once it was cancelled.
    """
    turn = (await _companion(ctx, _signals.status, step)())["answering"]
    if turn is not None:
        ctx.cancel(turn["invocationId"])
    return {"stopped": turn["invocationId"] if turn is not None else None}


def _followed(agent: AgentObject) -> None:
    """
    An agent's views beside those of every object: ``messages``, its
    conversation; ``clear``, which forgets it; and ``sync``, the conversation
    since ``cursor``, what the agent is doing, and the revision, in one
    request. And ``erase``, ``clear``'s work, which only its views call.
    """

    async def messages(ctx: ViewContext, request: MessagesRequest | None = None) -> Messages:
        """
        The conversation as clients read it, from its log: a page of it
        (``{limit, before}``), or what is new or changed since a cursor
        (``{cursor}``). It answers while a message is answered, with what was
        stored so far.
        """
        return await _log.page(ctx.db, request)

    async def erase(ctx: SessionContext) -> None:
        """``clear``'s work, once the handlers before it are done: the key's database empties, and its signals'."""
        start = _start_after(await _log.page(ctx.db, {"limit": 1}))
        async with ctx.db.transaction() as tx:
            await reset(tx)
            await _log.restart(tx, start)
        await cleared(ctx)
        await ctx.rpc(_signals.reset, key=_signals.signals_of(ctx), idempotency_key=f"{ctx.request.id}:reset")()

    async def clear(ctx: ViewContext) -> Stopped:
        """
        Forget the conversation: stop the message being answered (as
        ``stop``), then empty the key's database, and what its ``signals``
        hold. The log's ids and cursors go on, so a follower reads on, and
        drops the messages before ``first``. A message sent before it is
        answered first, then forgotten too.
        """
        stopped = await _stopped(ctx, "clear")
        await ctx.rpc(erase, key=ctx.key, idempotency_key=f"{ctx.request.id}:erase")()
        return stopped

    async def sync(ctx: ViewContext, request: SyncRequest | None = None) -> Synced:
        """
        The conversation since ``cursor`` (by default, all of it), what the
        agent is doing, and the revision; with ``revision``, once there is a
        newer one, or at the timeout. A client follows the conversation by
        sending each answer's ``revision`` and ``cursor`` back, at once while
        it has ``more``.
        """
        request = request or {}
        revision = request.get("revision")
        if revision is not None:
            watch: Watch = {"revision": revision, "timeoutSeconds": request.get("timeoutSeconds", 50)}
            await _companion(ctx, _signals.watch, "watch")(watch)
        # The revision first: a change after it shows in the next sync.
        now = await _companion(ctx, _signals.notifications)()
        status = await _companion(ctx, _signals.status)()
        page = await _log.page(ctx.db, {"cursor": request.get("cursor", 0), "limit": request.get("limit", 100)})
        synced: Synced = {**page, "revision": now["revision"], "status": status}
        return synced

    agent.view(messages)
    agent.handler(ingress_private=True)(erase)
    agent.view(inactivity_timeout=WAITS)(clear)
    agent.view(inactivity_timeout=WAITS)(sync)
