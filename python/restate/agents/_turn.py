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
to the clients that follow the conversation, copying the framework's
transcript into the log as it goes (``transcribing``).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from restate.agents import _log, _signals
from restate.agents._api import Turn
from restate.agents._calls import settle
from restate.agents._common import refusal_of
from restate.agents._transcript import Transcript
from restate.exceptions import TerminalError
from restate.sessions import SessionContext
from restate.sessions._link import Link
from restate.sessions.internal import ScopeKey, cancel_calls, cancelled, definition_of

T = typing.TypeVar("T")

Read = Callable[[], Awaitable[Transcript]]
"""A function that returns the framework's transcript as it stands, either new or one the framework keeps adding to."""


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
    """An agent's settings: whether it works unwatched (``background``), and what its ``clear`` also empties."""

    background: bool = False
    clear: Callable[[SessionContext], Awaitable[None]] | None = None


AGENT: Link[AgentInfo] = Link("an agent")
"""The agent's settings, attached to its definition."""


def _agent_of(ctx: SessionContext) -> AgentInfo | None:
    definition = definition_of(ctx)
    return AGENT.of(definition) if definition is not None else None


async def cleared(ctx: SessionContext) -> None:
    """Clear whatever the agent of ``ctx`` keeps outside its database."""
    agent = _agent_of(ctx)
    if agent is not None and agent.clear is not None:
        await agent.clear(ctx)


def _unfollowed(ctx: SessionContext) -> bool:
    """Return whether the agent of ``ctx`` runs in the background, publishing only when a turn ends."""
    agent = _agent_of(ctx)
    return agent is not None and agent.background


_READING: ScopeKey[Read] = ScopeKey("transcribing")
"""The transcript reader of the attempt's turn, while it answers."""

_WRITING: ScopeKey[asyncio.Future[None]] = ScopeKey("the log's writes")
"""The attempt's latest log write; writes run one after another, as parallel tool calls publish at the same time."""


def _logged(ctx: SessionContext, read: Read) -> Awaitable[None]:
    """Copy the transcript that ``read`` returns into the conversation's log, after any earlier write."""
    before = _WRITING.get(ctx)

    async def write() -> None:
        if before is not None:
            with contextlib.suppress(BaseException):
                await before
        transcript = await read()
        await _log.sync(ctx, transcript.messages())
        # Forget the calls whose results the framework has stored: see _calls.py.
        await settle(ctx, transcript.answered)

    done = asyncio.ensure_future(write())
    _WRITING.set(ctx, done)
    return done


async def publish(ctx: SessionContext) -> None:
    """Publish a change to the conversation of ``ctx``.

    While a framework is answering (inside ``transcribing``), the change is
    first copied into the log. Then the revision increases, and the clients
    that follow the conversation read it again.
    """
    read = _READING.get(ctx)
    if read is not None:
        await _logged(ctx, read)
    await ctx.call(_signals.publish, key=_signals.signals_of(ctx))


async def stepped(ctx: SessionContext) -> None:
    """Publish a step of the turn, unless the agent runs in the background."""
    if not _unfollowed(ctx):
        await publish(ctx)


async def tool_call_started(ctx: SessionContext) -> None:
    """Publish that a tool call of the turn is starting.

    Publishing also forgets the earlier calls whose results are stored, before
    the tool makes calls of its own. A background agent only forgets them,
    without publishing.
    """
    if not _unfollowed(ctx):
        await publish(ctx)
        return
    read = _READING.get(ctx)
    if read is not None:
        transcript = await read()
        await settle(ctx, transcript.answered)


async def tool_call_ended(ctx: SessionContext) -> None:
    """Publish that a tool call of the turn has ended, unless the agent runs in the background."""
    await stepped(ctx)


async def transcribing(ctx: SessionContext, read: Read, work: Callable[[], Awaitable[T]]) -> T:
    """Run ``work`` while keeping the conversation's log in step with the framework's transcript.

    ``work`` is a framework's turn (or a compaction, or a reset), and ``read``
    returns the framework's transcript as it stands. Every publish while
    ``work`` runs (such as a tool call's start and end) copies the transcript
    into the log, and so does the end of ``work``. The ``messages`` and
    ``sync`` views read the log.

    Args:
        ctx: The handler's context.
        read: Returns the framework's current transcript.
        work: The work to run.

    Returns:
        What ``work`` returns.
    """
    outer = _READING.get(ctx)
    _READING.set(ctx, read)
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
                await _logged(ctx, read)
        finally:
            if outer is not None:
                _READING.set(ctx, outer)
            else:
                _READING.delete(ctx)


def _now() -> str:
    return _signals.iso(time.time())


async def chat_turn(ctx: SessionContext, text: str, answer: Callable[[SessionContext, str], Awaitable[T]]) -> T:
    """Run ``answer`` as the turn of a ``chat`` handler.

    While it answers, the object's ``status`` reports the turn. Cancelling the
    invocation stops the turn (through ``stop_signal_of``) whenever it comes,
    and cancels the calls its tools made; the handler then fails with the
    cancellation. If the attempt ends first, the turn stops with
    ``AttemptEnded`` and the next attempt carries on. The turn publishes when
    it starts and when it ends.
    """
    invocation_id = ctx.request.id
    companion = _signals.signals_of(ctx)
    turn: Turn = {"invocationId": invocation_id, "since": _now()}
    # From here until the turn closes, the companion knows about it: `status` and `stop` ask it.
    await ctx.call(_signals.start, turn, key=companion)
    await stepped(ctx)

    async def end() -> None:
        # Copy the turn's last change into the log, then have the companion
        # close the turn, which publishes both as one revision. The close is a
        # call, awaited so that it is done before the turn ends.
        read = _READING.get(ctx)
        if read is not None:
            await _logged(ctx, read)
        await ctx.call(_signals.close, invocation_id, key=companion)

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
