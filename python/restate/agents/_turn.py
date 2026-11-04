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
The turn of a ``chat`` handler: its status, its cancellation, and publishing
for the clients that follow the conversation, with the framework's transcript
kept in the log as it goes. As sdk-sessions' ``chatHandler`` and
``transcribing`` (``common.ts``).
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
"""A framework's transcript as it stands, while it answers: a new one, or the one it keeps and adds to."""


class AttemptEnded(Exception):
    """
    Why a turn stopped when its attempt ended before it did: Restate suspended
    the attempt, gave up on it, or lost its connection. Nothing the attempt does
    after is stored, and the next attempt goes on with the turn, so it stops
    without answering: unlike a cancellation, nothing is cancelled.
    """

    def __init__(self) -> None:
        super().__init__("the attempt ended before its turn did")


def cancellation() -> TerminalError:
    """What a cancelled turn fails with: Restate's cancellation, 409."""
    return TerminalError("cancelled", status_code=409)


class Stop:
    """
    What stops a turn: a framework stops its work with it (a model's request,
    say). It stops with Restate's cancellation (``409``) once Restate cancels
    the invocation, the turn then ending as cancelled; or with an
    ``AttemptEnded`` once the attempt ends first, the work stopping and
    answering nothing.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: BaseException | None = None
        """Why it stopped, once it has."""

    @property
    def stopped(self) -> bool:
        """Whether it stopped."""
        return self.reason is not None

    def stop(self, reason: BaseException) -> None:
        """Stop, with ``reason``; once."""
        if self.reason is None:
            self.reason = reason
            self._event.set()

    async def wait(self) -> BaseException:
        """Return once it stopped: why."""
        await self._event.wait()
        assert self.reason is not None
        return self.reason

    async def guard(self, work: Awaitable[T]) -> T:
        """
        ``work``, unless it stops first: then ``work`` is cancelled, and this
        raises why it stopped. For a framework's request that the turn's stop
        must cut short.
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
    """The ``Stop`` of the turn of ``ctx``, a ``chat`` handler's: see ``Stop``."""
    return _STOP.get(ctx)


@dataclass(frozen=True)
class AgentInfo:
    """An agent's own: whether it works unwatched (``background``), and what its ``clear`` also empties."""

    background: bool = False
    clear: Callable[[SessionContext], Awaitable[None]] | None = None


AGENT: Link[AgentInfo] = Link("an agent")
"""An agent's own, on its definition."""


def _agent_of(ctx: SessionContext) -> AgentInfo | None:
    definition = definition_of(ctx)
    return AGENT.of(definition) if definition is not None else None


async def cleared(ctx: SessionContext) -> None:
    """Clear what the agent of ``ctx`` keeps beside its database."""
    agent = _agent_of(ctx)
    if agent is not None and agent.clear is not None:
        await agent.clear(ctx)


def _unfollowed(ctx: SessionContext) -> bool:
    """Whether the agent of ``ctx`` runs in the background: it publishes the turn's end only."""
    agent = _agent_of(ctx)
    return agent is not None and agent.background


_READING: ScopeKey[Read] = ScopeKey("transcribing")
"""The transcript of the attempt's turn, while it answers."""

_WRITING: ScopeKey[asyncio.Future[None]] = ScopeKey("the log's writes")
"""The attempt's last log write: they run one after another (tool calls in parallel publish at once)."""


def _logged(ctx: SessionContext, read: Read) -> Awaitable[None]:
    """The log of the conversation of ``ctx`` takes its transcript as ``read`` gives it."""
    before = _WRITING.get(ctx)

    async def write() -> None:
        if before is not None:
            with contextlib.suppress(BaseException):
                await before
        transcript = await read()
        await _log.sync(ctx, transcript.messages())
        # The calls whose results the framework stored are made: see _calls.py.
        await settle(ctx, transcript.answered)

    done = asyncio.ensure_future(write())
    _WRITING.set(ctx, done)
    return done


async def publish(ctx: SessionContext) -> None:
    """
    The conversation of ``ctx`` changed: its log takes the change (while a
    framework answers: ``transcribing``), its revision increases, and the
    clients that follow it read it again.
    """
    read = _READING.get(ctx)
    if read is not None:
        await _logged(ctx, read)
    await ctx.call(_signals.publish, key=_signals.signals_of(ctx))


async def stepped(ctx: SessionContext) -> None:
    """A step of a turn of ``ctx``: ``publish`` it, unless the agent runs in the background."""
    if not _unfollowed(ctx):
        await publish(ctx)


async def tool_call_started(ctx: SessionContext) -> None:
    """
    A tool call of the turn of ``ctx`` starts: ``publish`` it, which forgets
    the calls whose results are stored before the tool makes its own. In the
    background, it forgets them alone.
    """
    if not _unfollowed(ctx):
        await publish(ctx)
        return
    read = _READING.get(ctx)
    if read is not None:
        transcript = await read()
        await settle(ctx, transcript.answered)


async def tool_call_ended(ctx: SessionContext) -> None:
    """A tool call of the turn of ``ctx`` ended: ``publish`` it, unless the agent runs in the background."""
    await stepped(ctx)


async def transcribing(ctx: SessionContext, read: Read, work: Callable[[], Awaitable[T]]) -> T:
    """
    Run ``work``, a framework's turn (or a compaction, a reset), with ``read``
    giving the framework's transcript as it stands: each publish meanwhile (a
    tool call's start and end) takes it into the conversation's log, and so
    does ``work``'s end. ``messages`` and ``sync`` read the log.
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
            # A failed attempt stores nothing more: its retry runs again.
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
    """
    ``answer`` as a chat handler's turn. While it answers, the object's
    ``status`` says so (its ``Turn``). Its invocation's cancellation (Restate's,
    explicit) stops the turn (``stop_signal_of``) whenever it comes, and cancels
    the calls its tools made: the turn ends, and the handler fails with the
    cancellation. An attempt that ends first stops it too, with an
    ``AttemptEnded``: the work stops, and the next attempt goes on with it. It
    publishes when it starts and ends, for the clients that follow it.
    """
    invocation_id = ctx.request.id
    companion = _signals.signals_of(ctx)
    turn: Turn = {"invocationId": invocation_id, "since": _now()}
    # The companion knows the turn from here to its close: `status` and `stop` ask it.
    await ctx.call(_signals.start, turn, key=companion)
    await stepped(ctx)

    async def end() -> None:
        # The log takes the turn's last change, then the companion closes the
        # turn, which publishes it: one revision. The close is a call the turn
        # waits for, so that it is done before the turn ends.
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
            # Cancelled once answered: the answer stands.
        except TerminalError:
            await end()
            raise
        except Exception as error:
            # Any other error fails the attempt, and Restate retries the turn, but a refusal.
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
