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
"""The turn of a ``chat`` handler.

A turn reports its status, handles its cancellation, and publishes each change
to the clients that follow the conversation. The conversation itself is
already in the key's database, where the ``messages`` view reads it; a
publish only tells the clients to read again.

The layers: the conversation (``answer_once``) stores a turn, and knows
nothing of the companion; the agent's ``chat`` turn (``chat_turn``) starts,
publishes and closes it. A handler of one's own that prompts, on a session
object that is no agent, stores its turn and publishes nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from restate.agents import _signals
from restate.agents._api import Answering, Published, Steer
from restate.agents._calls import settle
from restate.agents._common import (
    BUDGET,
    WINDOW,
    CompactHistory,
    Summarize,
    Window,
    after_steps,
    flush_held,
    refusal_of,
)
from restate.exceptions import TerminalError
from restate.sessions import SessionContext
from restate.sessions._link import Link
from restate.sessions.internal import ScopeKey, cancel_calls, cancelled, definition_of

T = typing.TypeVar("T")

Stored = Callable[[str, str | None], bool]
"""Whether a tool call's result is stored: ``stored(call_id, failed)``, where ``failed`` is the error the call returned."""


class AttemptEnded(Exception):
    """Raised to stop a turn whose attempt ended before the turn did.

    The attempt ends when Restate suspends it, gives up on it, or loses its
    connection. Nothing the attempt does afterwards is stored, and the next
    attempt carries on with the turn, so the turn stops without answering.
    Unlike a cancellation, nothing is cancelled.
    """

    def __init__(self) -> None:
        super().__init__("the attempt ended before its turn did")


def cancellation() -> TerminalError:
    """Return the error a cancelled turn fails with: Restate's cancellation, status 409."""
    return TerminalError("cancelled", status_code=409)


class Stop:
    """The stop signal of a turn, which a framework uses to cut its work short (a model request, for example).

    It stops with Restate's cancellation (status 409) when Restate cancels the
    invocation, and the turn then ends as cancelled. It stops with
    ``AttemptEnded`` when the attempt ends first, and the work then stops
    without answering.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: BaseException | None = None
        """Why it stopped, or ``None`` while it has not."""

    @property
    def stopped(self) -> bool:
        """Whether it has stopped."""
        return self.reason is not None

    def stop(self, reason: BaseException) -> None:
        """Stop with ``reason``, unless already stopped."""
        if self.reason is None:
            self.reason = reason
            self._event.set()

    async def wait(self) -> BaseException:
        """Wait until it stops, and return the reason."""
        await self._event.wait()
        assert self.reason is not None
        return self.reason

    async def guard(self, work: Awaitable[T]) -> T:
        """Await ``work`` and return its result, unless the turn stops first.

        If it stops first, ``work`` is cancelled and the stop reason is
        raised. Use it for a framework request that stopping the turn must cut
        short.
        """
        task = asyncio.ensure_future(work)
        stopping = asyncio.ensure_future(self._event.wait())
        try:
            await asyncio.wait({task, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
        if task.done():
            return task.result()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        assert self.reason is not None
        raise self.reason


_STOP: ScopeKey[Stop] = ScopeKey("stop_signal_of")


def stop_signal_of(ctx: SessionContext) -> Stop | None:
    """Return the ``Stop`` of the ``chat`` turn running on ``ctx``, or ``None`` outside one."""
    return _STOP.get(ctx)


@dataclass(frozen=True)
class AgentInfo:
    """An agent's settings: whether it works unwatched (``background``), what its ``clear`` also empties, and how
    much of the conversation its model sees (``window`` and ``budget``, see ``_conversation.py``)."""

    background: bool = False
    clear: Callable[[SessionContext], Awaitable[None]] | None = None
    window: Window = WINDOW
    budget: int = BUDGET
    summarize: Summarize | None = None
    """How the agent summarizes the turns before the window, past ``budget``: without, they are only trimmed."""
    snapshots: bool = False
    """Whether the agent takes its history from snapshots (see ``Snapshot``): ``window``, ``budget`` and
    ``summarize`` then do not apply."""
    compact_history: CompactHistory | None = None
    """With ``snapshots``, how the agent compacts its history between turns, when a turn asks for it."""
    steers: bool = False
    """Whether the agent's framework takes steered messages at a turn's steps (``take_steers``): without, each
    message steered to the agent is answered as a turn of its own. A background agent takes none."""


AGENT: Link[AgentInfo] = Link("an agent")
"""The agent's settings, attached to its definition."""


def _agent_of(ctx: SessionContext) -> AgentInfo | None:
    definition = definition_of(ctx)
    return AGENT.of(definition) if definition is not None else None


def settings_of(ctx: SessionContext) -> AgentInfo:
    """Return the settings of the agent of ``ctx``: the defaults, for a session object that is no agent."""
    return _agent_of(ctx) or AgentInfo()


async def cleared(ctx: SessionContext) -> None:
    """Clear whatever the agent of ``ctx`` keeps outside its database."""
    agent = _agent_of(ctx)
    if agent is not None and agent.clear is not None:
        await agent.clear(ctx)


def _unfollowed(ctx: SessionContext) -> bool:
    """Return whether the agent of ``ctx`` runs in the background, publishing only when a turn ends."""
    agent = _agent_of(ctx)
    return agent is not None and agent.background


@dataclass
class Inbox:
    """The messages steered to a ``chat`` turn that it has not taken yet, as the companion last published them.

    The companion returns them from every ``publish``, which a turn makes as
    it starts and after each step it stores that a tool waits for (a reply
    with tool calls, the tools' results): so the turn knows of them without a
    call of its own, and finds them at its next step (``take_steers``). The
    newest revision wins, as writes that run at once publish at once.
    """

    revision: int = -1
    messages: list[Steer] = field(default_factory=list[Steer])

    def saw(self, published: Published) -> None:
        """Keep what ``publish`` returned, if it is newer than what is kept."""
        revision = published["revision"]
        if revision > self.revision:
            self.revision = revision
            self.messages = list(published.get("steering", []))


_INBOX: ScopeKey[Inbox] = ScopeKey("inbox")


def inbox_of(ctx: SessionContext) -> Inbox | None:
    """Return the inbox of the ``chat`` turn running on ``ctx``, if it takes steered messages: ``None`` outside one."""
    return _INBOX.get(ctx)


_ANSWERING: ScopeKey[Stored] = ScopeKey("answering")
"""What the attempt's turn has stored, while it answers."""

_SETTLING: ScopeKey[asyncio.Future[None]] = ScopeKey("settling")
"""The attempt's latest settling of calls; they run one after another, as parallel tool calls settle at once."""


def _settled(ctx: SessionContext, stored: Stored) -> Awaitable[None]:
    """Forget the calls whose results the conversation has stored (see ``_calls.py``), after any earlier settling."""
    before = _SETTLING.get(ctx)

    async def run() -> None:
        if before is not None:
            with contextlib.suppress(BaseException):
                await before
        await settle(ctx, stored)

    done = asyncio.ensure_future(run())
    _SETTLING.set(ctx, done)
    return done


async def publish(ctx: SessionContext) -> None:
    """Publish a change to the conversation of ``ctx``: the revision increases, and the clients read it again.

    The rows the conversation holds back are written first: the clients
    read them once told to. It forgets no calls: ``answer_once`` does that
    after each write of steps that commits, before it publishes there
    (``settled``), and the ``settle`` handler publishes outside a turn.

    Only an agent publishes, to its companion: after each step its handlers'
    turns store (``publish_steps``), and its ``settle``. A session object that
    is no agent has no companion, and nothing follows it: its turns are only
    stored.

    Raises:
        TypeError: If ``ctx`` is not the context of an agent's handler.
    """
    companion = _signals.companion_in(ctx)
    await flush_held(ctx)
    published = await ctx.call(companion.publish, key=ctx.key)
    inbox = _INBOX.get(ctx)
    if inbox is not None:
        inbox.saw(published)


def publish_steps(ctx: SessionContext) -> None:
    """Have each write of steps that commits, in the turns that ``ctx``'s handler answers, publish: unless the
    agent runs in the background, which publishes only as a ``chat`` turn ends.

    ``chat_turn`` calls it, and so does every handler of an agent
    (``AgentObject``), a handler of your own that prompts included: its
    followers see its steps as they are stored. ``answer_once`` runs it
    (``after_steps``), and knows nothing of it.
    """
    if not _unfollowed(ctx):
        after_steps(ctx, lambda: publish(ctx))


async def settled(ctx: SessionContext) -> None:
    """Forget the calls whose results the turn being answered (inside ``answering``) has stored (see ``_calls.py``).

    ``answer_once`` has it done after each write of the turn's steps that
    commits, before what its caller set (``after_steps``), and each tool
    call's start does it. Outside ``answering``, it does nothing.
    """
    stored = _ANSWERING.get(ctx)
    if stored is not None:
        await _settled(ctx, stored)


async def tool_call_started(ctx: SessionContext) -> None:
    """Forget the earlier calls whose results are stored, before a tool call of the turn makes calls of its own.

    It publishes nothing: the turn publishes once the tool call is stored
    (with its reply), and once its result is.
    """
    await settled(ctx)


async def tool_call_ended(ctx: SessionContext) -> None:
    """Do nothing: kept for integrations of an earlier version, which called it as a tool call ended.

    The turn publishes once the call's result is stored, not as the call ends.
    """


async def answering(ctx: SessionContext, stored: Stored, work: Callable[[], Awaitable[T]]) -> T:
    """Run ``work``, a framework's turn, forgetting the calls whose results it stores as it goes.

    Every publish while ``work`` runs (after a step is stored), each tool
    call's start, and the end of ``work``, forget the Restate calls of the
    tool calls whose results ``stored`` says are stored (see ``_calls.py``).

    Args:
        ctx: The handler's context.
        stored: Whether a tool call's result is stored, such as ``Conversation.stored``.
        work: The work to run.

    Returns:
        What ``work`` returns.
    """
    outer = _ANSWERING.get(ctx)
    _ANSWERING.set(ctx, stored)
    failed = False
    try:
        return await work()
    except BaseException as error:
        failed = not isinstance(error, TerminalError)
        raise
    finally:
        try:
            # A failed attempt stores nothing more, as its retry runs again.
            if not failed:
                await _settled(ctx, stored)
        finally:
            if outer is not None:
                _ANSWERING.set(ctx, outer)
            else:
                _ANSWERING.delete(ctx)


def _now() -> str:
    return _signals.iso(time.time())


async def chat_turn(ctx: SessionContext, text: str, answer: Callable[[SessionContext, str], Awaitable[T]]) -> T:
    """Run ``answer`` as the turn of a ``chat`` handler.

    While it answers, the object's ``status`` reports the turn. Cancelling the
    invocation stops the turn (through ``stop_signal_of``) whenever it comes,
    and cancels the calls its tools made; the handler then fails with the
    cancellation. If the attempt ends first, the turn stops with
    ``AttemptEnded`` and the next attempt carries on. The turn publishes when
    it starts (the companion's ``start`` does), after each write of its steps
    that commits (a reply with tool calls, the tools' results: ``answer_once``
    runs what ``after_steps`` sets, see ``Conversation.hold``), unless the
    agent runs in the background, and when it ends (its ``close``). The
    conversation itself only stores: a handler of one's own that prompts
    stores its turn and publishes nothing.

    An error ends the turn for good if it is a ``TerminalError``, or a model
    provider's refusal (``refusal_of``), as a 424. Any other error fails the
    attempt, and Restate retries the turn. So each integration raises a
    ``TerminalError`` for its framework's errors that a retry would meet
    again: OpenAI's ``MaxTurnsExceeded``, guardrail tripwires, setup errors
    and runs stopped for approvals, ADK's model error codes and limit of model
    calls, LangGraph's recursion limit and invalid updates, Pydantic
    AI's usage limits, retries that ran out and setup errors.
    """
    invocation_id = ctx.request.id
    companion = _signals.companion_in(ctx)
    turn: Answering = {"invocationId": invocation_id, "since": _now()}
    settings = settings_of(ctx)
    steers = settings.steers and not settings.background
    if steers:
        # Before the first publish, which may return messages steered to the turn already.
        _INBOX.set(ctx, Inbox())
    # From here until the turn closes, the companion knows about it: `status` and `stop` ask it. Once the
    # turn's invocation is gone, the companion sends it to the agent's `settle` (which `chat()` adds).
    followed = not _unfollowed(ctx)
    started: _signals.Started = {**turn, "steers": steers, "publishes": followed}
    # A followed turn's start is its first publish; a background agent's publishes nothing (and takes no steers).
    published = await ctx.call(companion.start, started, key=ctx.key)
    inbox = _INBOX.get(ctx)
    if inbox is not None and published is not None:
        inbox.saw(published)
    # Each step the turn stores that a tool waits for (a reply with tool calls, the tools' results).
    publish_steps(ctx)

    async def end() -> None:
        # Have the companion close the turn, which publishes its last change
        # with it, as one revision. The close is a call, awaited so that it is
        # done before the turn ends.
        stored = _ANSWERING.get(ctx)
        if stored is not None:
            await _settled(ctx, stored)
        await ctx.call(companion.close, invocation_id, key=ctx.key)

    stop = Stop()
    _STOP.set(ctx, stop)

    async def on_cancel() -> None:
        await cancelled(ctx)
        stop.stop(cancellation())
        await cancel_calls(ctx)

    async def on_attempt_end() -> None:
        await ctx.request.attempt_finished_event.wait()
        stop.stop(AttemptEnded())

    watchers = [asyncio.ensure_future(on_cancel()), asyncio.ensure_future(on_attempt_end())]
    try:
        try:
            answered = await answer(ctx, text)
            # A cancellation after the answer does not change it.
        except TerminalError:
            await end()
            raise
        except Exception as error:
            # Any other error fails the attempt and Restate retries the turn, unless it is a refusal.
            terminal = refusal_of(ctx, error)
            if terminal is None:
                raise
            await end()
            raise terminal from error
        await end()
        return answered
    finally:
        for watcher in watchers:
            watcher.cancel()
