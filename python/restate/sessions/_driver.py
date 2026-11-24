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
"""One attempt of a session object's handler, on the explicit recovery VM (``PyExplicitRecoveryVM``).

The VM never replays a journal: every attempt runs the handler from the start.
An action returns a handle, and its result comes back as a notification of that
handle, which settles the action's future here. Two things move the attempt
along:

- **The input pump**, the only reader of the request body: it hands the VM
  each chunk, and the end of the body.
- **The tick**, scheduled after every call to the VM (once per turn of the
  event loop): it writes the VM's output, settles the futures, and notices the
  end of the attempt.

Restate shuts an attempt down by closing the request body: from then on every
action raises ``SuspendedException``, and the futures still pending fail with
it. A new attempt runs the handler from the start.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import operator
import os
import traceback
import typing
from collections.abc import AsyncIterator, Awaitable, Callable, Generator
from datetime import timedelta
from typing import Any

from restate._internal import (  # pylint: disable=no-name-in-module
    AttemptShutDownException,
    PyExplicitRecoveryVM,
    PyFailure,
    PyHeader,
    PyInput,
    PyStateKeys,
    PyVoid,
    VMException,
)
from restate.context import AttemptFinishedEvent, RestateDurableCallFuture
from restate.exceptions import (
    RetryableError,
    SdkInternalBaseException,
    SdkInternalException,
    SuspendedException,
    TerminalError,
)
from restate.server_types import ReceiveChannel, Send

logger = logging.getLogger(__name__)

T = typing.TypeVar("T")

HEARTBEAT_INTERVAL = timedelta(seconds=20)
"""How often an attempt in ``working`` tells Restate that it makes progress.

Restate shuts down an attempt it heard nothing from for its inactivity timeout
(a minute, by default). That is the default: the attempt suspends on what it
waits for (or, waiting for nothing, starts again at once), and a wait of days
holds no process. Work that must not be cut off (a model request) says so with
``working``: while any of it runs, the attempt heartbeats, whatever else it
waits for. A deployment that shortens the timeout must shorten this with it
(``RESTATE_SESSIONS_HEARTBEAT``, in milliseconds, sets it).
"""


def heartbeat_interval() -> float:
    """The heartbeat interval in seconds: ``RESTATE_SESSIONS_HEARTBEAT`` (milliseconds), or ``HEARTBEAT_INTERVAL``.

    A value that is not a positive, finite number is ignored.
    """
    default = HEARTBEAT_INTERVAL.total_seconds()
    raw = os.environ.get("RESTATE_SESSIONS_HEARTBEAT", "")
    try:
        millis = float(raw) if "_" not in raw else math.nan
    except ValueError:
        return default
    return millis / 1000 if math.isfinite(millis) and millis > 0 else default


SYNC_KEY = "v22/sync"
"""The state key a sync clears with an ack, under the state format's prefix, which tooling hides."""


def _quiet(future: asyncio.Future[Any]) -> asyncio.Future[Any]:
    """Keep asyncio from logging the exception of ``future`` if nothing awaits it."""
    future.add_done_callback(lambda f: f.cancelled() or f.exception())
    return future


def _settle(future: asyncio.Future[Any], value: Any) -> None:
    """Settle ``future`` with a notification's value."""
    if future.done():
        return
    if isinstance(value, PyVoid):
        future.set_result(None)
    elif isinstance(value, PyFailure):
        future.set_exception(TerminalError(value.message, value.code, metadata=dict(value.metadata or [])))
    elif isinstance(value, PyStateKeys):
        future.set_result(list(value.keys))
    else:
        future.set_result(value)


class AttemptFinished(AttemptFinishedEvent):
    """The attempt's ``Request.attempt_finished_event``: set when the attempt ends."""

    def __init__(self, event: asyncio.Event) -> None:
        super().__init__()
        self._event: asyncio.Event = event

    def is_set(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


class AttemptDriver:
    """Drives one attempt over its VM: the input pump, the tick, and the futures of the actions."""

    def __init__(
        self, vm: PyExplicitRecoveryVM, send: Send, receive: ReceiveChannel, explicit_cancellation: bool
    ) -> None:
        self.vm: PyExplicitRecoveryVM = vm
        self._send: Send = send
        self._receive: ReceiveChannel = receive
        self._explicit_cancellation: bool = explicit_cancellation
        self._loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        self._futures: dict[int, asyncio.Future[Any]] = {}
        # The handles a cancellation leaves to settle, and is never carried to: the invocation
        # ids of the calls and sends, and the reads and acks of the database's store.
        self._left_to_settle: set[int] = set()
        # A cancellation that failed nothing the handler awaits, carried to the next thing it
        # does: a result of Restate's (``future``), or a call of its database
        # (``take_carried_cancellation``). A retry of a cancelled invocation knows of it before
        # the handler starts, and its first await fails all the same. There is one carry, here.
        self._carried_cancellation: bool = False
        self.calls: list[_CallFuture] = []
        """The attempt's calls, for ``cancel_calls``."""
        # The calls Restate has yet to answer, by the handle of their result.
        self._unanswered: dict[int, _CallFuture] = {}
        # The cancellation of the calls whose invocation id a cancellation waits for.
        self._cancelling: asyncio.Future[Any] | None = None
        self._queued: bool = False
        self._output: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._writer: asyncio.Task[None] = asyncio.ensure_future(self._write_output())
        self._pump: asyncio.Task[None] | None = None
        self._handler: asyncio.Future[bytes] | None = None
        # The ``working`` scopes open: the attempt heartbeats while there is one.
        self._scopes: int = 0
        # The one timer of the heartbeats, armed for ``_beat_due``. It outlives a run of scopes, so the database's
        # scopes (one a statement) do not start and cancel a timer each: it fires, finds no scope open, and is not
        # armed again.
        self._heartbeats: asyncio.TimerHandle | None = None
        self._beat_due: float = 0.0
        # The run of scopes now open: its interval, and when its next heartbeat is due.
        self._run_every: float = 0.0
        self._next_beat: float = 0.0
        # The heartbeat interval, read from the environment on first use.
        self._beat_every: float | None = None
        # When the attempt last wrote to Restate, which restarts Restate's inactivity timer.
        self._wrote_at: float = self._loop.time()
        self._end_listeners: list[Callable[[], None]] = []
        self.finished: asyncio.Event = asyncio.Event()
        self.ended: BaseException | None = None
        """Why the attempt ended, once it did: ``SuspendedException`` for a shutdown or the end of the handler."""
        self.cancellation: asyncio.Future[None] = _quiet(self._loop.create_future())
        """Resolved once Restate cancels the invocation; cancelled if the attempt ends first."""
        vm.set_scheduler(self.schedule)

    # --- Start and end

    async def start(self) -> PyInput | None:
        """Read the start of the invocation and start the handler: its request, or ``None`` if it can't start."""
        try:
            while not self.vm.is_ready_to_execute():
                message = await self._receive()
                if message.get("type") == "http.disconnect":
                    return None
                if message.get("type") == "http.request":
                    body = message.get("body", b"")
                    if body:
                        self.vm.notify_input(typing.cast(bytes, body))
                    if not message.get("more_body", False):
                        self.vm.notify_input_closed()
            request = self.vm.input()
        except (VMException, AttemptShutDownException):
            # The VM wrote why.
            self.schedule()
            return None
        self._pump = asyncio.ensure_future(self._read_input())
        # The failures of the traces earlier attempts left open, and an earlier cancellation.
        self.schedule()
        return request

    async def run(self, handler: Callable[[], Awaitable[bytes]]) -> None:
        """Run the handler, and end the attempt with its output or error.

        If the attempt is shut down or fails first, the handler's task is cancelled: its local work
        (a model request, say) stops, since nothing it does would count any more.
        """
        self._handler = asyncio.ensure_future(self._cancelled_calls_first(handler))
        try:
            output = await self._handler
            self._last(lambda vm: vm.end_success(output))
        except TerminalError as t:
            failure = _failure_of(t)
            self._last(lambda vm: vm.end_failure(failure))
        except RetryableError as r:
            self._fail(r.message, r, r.retry_after)
        except SdkInternalBaseException:
            # The attempt ended: the VM wrote why.
            pass
        except asyncio.CancelledError:
            if self.ended is None or asyncio.current_task().cancelling():  # type: ignore[union-attr]
                raise
            # The attempt ended, and stopped the handler.
        except Exception as e:  # pylint: disable=broad-exception-caught
            cause: BaseException | None = e
            while cause is not None:
                if isinstance(cause, TerminalError):
                    self._last(operator.methodcaller("end_failure", _failure_of(cause)))
                    return
                if isinstance(cause, RetryableError):
                    self._fail(cause.message, cause, cause.retry_after)
                    return
                if isinstance(cause, SdkInternalBaseException):
                    return
                cause = cause.__cause__
            logger.exception("Exception in Restate handler")
            self._fail(repr(e), e, None)

    async def _cancelled_calls_first(self, handler: Callable[[], Awaitable[bytes]]) -> bytes:
        """Run the handler; before it ends, the calls a cancellation cancels are."""
        try:
            return await handler()
        finally:
            if self._cancelling is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.shield(self._cancelling)

    def _last(self, f: Callable[[PyExplicitRecoveryVM], None]) -> None:
        """End the attempt with ``f``, unless it ended already."""
        with contextlib.suppress(VMException, AttemptShutDownException):
            f(self.vm)
        self.schedule()

    def _fail(self, message: str, error: BaseException, retry_after: timedelta | None) -> None:
        stacktrace = "".join(traceback.format_exception(error))
        delay = int(retry_after.total_seconds() * 1000) if retry_after is not None else None
        self._last(lambda vm: vm.notify_error(message, stacktrace, delay))

    def abandon(self) -> None:
        """Stop the attempt's tasks without writing anything more: the server is going away."""
        self._end(SuspendedException())
        self._writer.cancel()
        if self._pump is not None:
            self._pump.cancel()

    async def leave(self) -> None:
        """Write the rest of the output, wait for Restate to close the request, then close the response."""
        self._tick()
        self._end(SuspendedException())
        self._output.put_nowait(None)
        await self._writer
        await self._receive.block_until_http_input_closed()
        if self._pump is not None:
            self._pump.cancel()
        await self._send({"type": "http.response.body", "body": b"", "more_body": False})

    # --- Input and output

    async def _read_input(self) -> None:
        """The input pump: hand the VM the request body, until it ends."""
        while True:
            message = await self._receive()
            kind = message.get("type")
            if kind == "http.disconnect":
                self.vm.notify_input_closed()
                self.schedule()
                return
            if kind != "http.request":
                continue
            body = message.get("body", b"")
            if body:
                self.vm.notify_input(typing.cast(bytes, body))
            if not message.get("more_body", False):
                self.vm.notify_input_closed()
                self.schedule()
                return
            self.schedule()

    async def _write_output(self) -> None:
        """Write the output, in order, until ``leave``."""
        while (chunk := await self._output.get()) is not None:
            try:
                await self._send({"type": "http.response.body", "body": chunk, "more_body": True})
            except Exception:  # pylint: disable=broad-exception-caught
                logger.debug("Could not write the output: the connection is gone", exc_info=True)

    def schedule(self) -> None:
        """Schedule a tick, once per turn of the event loop."""
        if not self._queued:
            self._queued = True
            self._loop.call_soon(self._tick)

    def _tick(self) -> None:
        """Write the VM's output, settle the futures of the completed actions, and notice the end of the attempt."""
        self._queued = False
        output = self.vm.take_output()
        if output:
            self._output.put_nowait(output)
            self._wrote_at = self._loop.time()
        if self.ended is not None:
            return
        try:
            while (notification := self.vm.take_notification()) is not None:
                handle, value = notification
                self._left_to_settle.discard(handle)
                call = self._unanswered.pop(handle, None)
                if call is not None:
                    call.answered = True
                future = self._futures.pop(handle, None)
                if future is not None:
                    _settle(future, value)
        except AttemptShutDownException:
            self._end(SuspendedException())
        except VMException as error:
            logger.debug("The attempt failed: %s", error)
            self._end(SdkInternalException())
        else:
            # The VM tells whether a cancellation arrived, in this attempt or an earlier one: acted
            # on once.
            if not self.cancellation.done() and self.vm.is_cancelled():
                self._cancelled()
            if self.vm.state() == "closed":
                self._end(SuspendedException())

    def _end(self, reason: BaseException) -> None:
        """The attempt ended: fail what still waits, and tell the listeners."""
        if self.ended is not None:
            return
        self.ended = reason
        futures, self._futures = self._futures, {}
        for future in futures.values():
            if not future.done():
                future.set_exception(reason)
        self.cancellation.cancel()
        if self._handler is not None:
            self._handler.cancel()
        if self._heartbeats is not None:
            self._heartbeats.cancel()
        self.finished.set()
        listeners, self._end_listeners = self._end_listeners, []
        for listener in listeners:
            listener()

    def on_end(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call ``listener`` when the attempt ends; return a function that unregisters it."""
        if self.ended is not None:
            listener()
            return lambda: None
        self._end_listeners.append(listener)

        def cancel() -> None:
            if listener in self._end_listeners:
                self._end_listeners.remove(listener)

        return cancel

    # --- Actions

    def act(self, f: Callable[[PyExplicitRecoveryVM], T]) -> T:
        """Call the VM with ``f``, then schedule a tick; the attempt's end raises ``SuspendedException``."""
        if self.ended is not None:
            raise self.ended
        try:
            return f(self.vm)
        except AttemptShutDownException:
            raise SuspendedException() from None
        except VMException as error:
            raise SdkInternalException() from error
        finally:
            self.schedule()

    def future(self, handle: int) -> asyncio.Future[Any]:
        """The future the notification of ``handle`` settles: a result the handler awaits.

        A carried cancellation fails it, once.
        """
        if self.ended is None and self._carried_cancellation:
            self._carried_cancellation = False
            future = _quiet(self._loop.create_future())
            future.set_exception(_cancelled_error())
            return future
        return self._future(handle)

    def invocation_id_future(self, handle: int) -> asyncio.Future[Any]:
        """The future of a call's or send's invocation id, which a cancellation leaves to settle.

        Cancelling the call needs it; and a handler rarely awaits it, so a cancellation carried
        to it would be lost.
        """
        self._left_to_settle.add(handle)
        return self._future(handle)

    def store_future(self, handle: int) -> asyncio.Future[Any]:
        """The future of a read or an ack of the database's store, which a cancellation leaves to settle.

        Restate answers those promptly, and failing the ack of a commit whose writes are out
        would report as cancelled a commit that landed. A call of the database gets a
        cancellation only as the handler makes it, before it issues anything (see
        ``Attempt.asked``); one under way runs to its end. The store's waits in Rust
        (``wait_in_rust``) are left alone in the same way: this is the store of an endpoint
        with a journal codec.
        """
        self._left_to_settle.add(handle)
        return self._future(handle)

    def _future(self, handle: int) -> asyncio.Future[Any]:
        future = _quiet(self._loop.create_future())
        if self.ended is not None:
            future.set_exception(self.ended)
        else:
            self._futures[handle] = future
        return future

    def take_carried_cancellation(self) -> bool:
        """Whether a cancellation is owed to the handler's next await, taking it.

        The caller delivers it: the handler's call of its database, before it issues anything,
        so that a call that fails with the cancellation did nothing.
        """
        if self.ended is not None:
            # The attempt is over: what the handler does now fails with that, as its awaits of
            # Restate do, and the next attempt gets the cancellation.
            return False
        carried, self._carried_cancellation = self._carried_cancellation, False
        return carried

    def track(self, call: _CallFuture, result_handle: int) -> None:
        """A call the attempt made, which a cancellation cancels until Restate answers it."""
        self.calls.append(call)
        self._unanswered[result_handle] = call

    def _cancelled(self) -> None:
        """Restate cancelled the invocation."""
        if not self.cancellation.done():
            self.cancellation.set_result(None)
        if self._explicit_cancellation:
            return
        # The calls in flight are cancelled (right away those whose invocation id is known, the
        # others as it comes: the handler's end waits for them), and the awaits fail.
        for call in list(self.calls):
            if not call.answered and call.knows_invocation_id():
                with contextlib.suppress(SdkInternalBaseException):
                    self.act(operator.methodcaller("cancel_invocation", call.known_invocation_id()))
        self._cancelling = _quiet(asyncio.ensure_future(self.cancel_calls()))
        cancelled = _cancelled_error()
        failed_a_result = False
        for handle, future in list(self._futures.items()):
            if handle not in self._left_to_settle:
                del self._futures[handle]
                if not future.done():
                    future.set_exception(cancelled)
                    failed_a_result = True
        # Carried only if nothing the handler awaits was failed: it is then still owed its one.
        self._carried_cancellation = not failed_a_result

    async def cancel_calls(self) -> list[str]:
        """Cancel the calls of the attempt that Restate has not answered; return their invocation ids.

        Not answered, rather than not done: a cancellation fails the result of a call the handler
        awaits, which leaves the callee running, and that is the call to cancel.
        """
        cancelled: list[str] = []
        for call in list(self.calls):
            if call.answered or call.cancelled:
                continue
            try:
                invocation_id = await call.invocation_id()
            except BaseException:  # pylint: disable=broad-exception-caught
                continue
            if not call.answered and not call.cancelled:
                call.cancelled = True
                self.act(operator.methodcaller("cancel_invocation", invocation_id))
                cancelled.append(invocation_id)
        return cancelled

    # --- Keeping the attempt alive

    @contextlib.asynccontextmanager
    async def working(self) -> AsyncIterator[None]:
        """Keep the attempt alive while the body runs (see ``HEARTBEAT_INTERVAL``).

        One heartbeat every interval, from the first scope opening to the last
        closing. The first comes once the attempt has been silent for an
        interval (at once, if it already has), so the silence never reaches more
        than that; a scope that closes sooner (the database's, around each of its
        calls) sends nothing. One timer serves scopes that follow one another.

        Raises:
            RuntimeError: Entered on another event loop than the attempt's (a
                thread's own loop): the heartbeat is the attempt's.
        """
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("ctx.working() must be entered on the attempt's own event loop, not another thread's")
        if self._beat_every is None:
            self._beat_every = heartbeat_interval()
        self._scopes += 1
        if self._scopes == 1:
            self._start_heartbeats(self._beat_every)
        try:
            yield
        finally:
            self._scopes -= 1

    def _start_heartbeats(self, every: float) -> None:
        """A run of scopes opens: its first beat is due once the attempt has been silent for ``every`` (at once, if it
        has), and the next ``every`` after each beat, while a scope stays open.

        The timer is armed for the first. On firing it beats if the run it finds is that old, waits out the rest if a
        run that opened later is younger, and is let go if no scope is open.
        """
        if self.ended is not None:
            return
        now = self._loop.time()
        # The first once the silence reaches an interval: at once, if it already has.
        first = self._wrote_at + every
        if first <= now:
            self._heartbeat()
            first = now + every
        self._run_every = every
        self._next_beat = first
        self._arm(first)

    def _arm(self, due: float) -> None:
        """Arm the timer for ``due``, unless it is armed for that or earlier."""
        if self._heartbeats is not None:
            if self._beat_due <= due:
                return
            self._heartbeats.cancel()
        self._beat_due = due
        self._heartbeats = self._loop.call_at(due, self._beat)

    def _beat(self) -> None:
        """The timer fired."""
        self._heartbeats = None
        if self.ended is not None or self._scopes == 0:
            return
        now = self._loop.time()
        if now >= self._next_beat:
            self._heartbeat()
            self._next_beat = now + self._run_every
        self._arm(self._next_beat)

    def _heartbeat(self) -> None:
        """Tell Restate the attempt makes progress."""
        # If the attempt ended, the tick notices.
        with contextlib.suppress(SdkInternalBaseException):
            self.act(lambda vm: vm.heartbeat())
            # The write restarts Restate's inactivity timer, as the tick will note: scopes opened before it do not beat
            # again.
            self._wrote_at = self._loop.time()


def _cancelled_error() -> TerminalError:
    """What an await fails with when Restate cancelled the invocation, as the SDK's own awaits do."""
    return TerminalError("cancelled", 409)


def _failure_of(error: TerminalError) -> Any:
    return PyFailure(error.status_code, error.message, None, list((error.metadata or {}).items()) or None)


def _headers_of(headers: dict[str, str] | None) -> list[Any] | None:
    return [PyHeader(key, value) for key, value in headers.items()] if headers else None


class _CallFuture(RestateDurableCallFuture[bytes]):
    """The result of a call, its invocation id, and its cancellation."""

    def __init__(
        self, driver: AttemptDriver, invocation_id: asyncio.Future[str], result: asyncio.Future[bytes]
    ) -> None:
        super().__init__()
        self._driver: AttemptDriver = driver
        self._invocation_id: asyncio.Future[str] = invocation_id
        self._result: asyncio.Future[bytes] = result

        self.cancelled: bool = False
        """Whether the attempt cancelled the call's invocation."""
        self.answered: bool = False
        """Whether Restate answered the call: its result came, the callee's value or its failure."""

    def done(self) -> bool:
        return self._result.done()

    def knows_invocation_id(self) -> bool:
        return (
            self._invocation_id.done()
            and not self._invocation_id.cancelled()
            and self._invocation_id.exception() is None
        )

    def known_invocation_id(self) -> str:
        self.cancelled = True
        return self._invocation_id.result()

    def __await__(self) -> Generator[Any, Any, bytes]:
        return self._result.__await__()

    async def invocation_id(self) -> str:
        return await asyncio.shield(self._invocation_id)

    async def cancel_invocation(self) -> None:
        invocation_id = await self.invocation_id()
        self._driver.act(lambda vm: vm.cancel_invocation(invocation_id))


class _SendHandle:
    """A send: its invocation id, once Restate stored it, and its cancellation."""

    def __init__(self, driver: AttemptDriver, invocation_id: asyncio.Future[str]) -> None:
        self._driver: AttemptDriver = driver
        self._invocation_id: asyncio.Future[str] = invocation_id

    async def invocation_id(self) -> str:
        return await asyncio.shield(self._invocation_id)

    async def cancel_invocation(self) -> None:
        invocation_id = await self.invocation_id()
        self._driver.act(lambda vm: vm.cancel_invocation(invocation_id))


class VmHost:
    """The ``SqlHost`` of an attempt on Restate: its actions are the VM's.

    The database reads and writes the VM directly (``vm``), and waits for it in
    Rust. With a journal codec, which only Python applies, ``vm`` is ``None``
    and the database goes through ``get``, ``set`` and ``clear``.
    """

    def __init__(self, driver: AttemptDriver, *, service: str, key: str, shared: bool, codec: Any = None) -> None:
        self.driver: AttemptDriver = driver
        self.service: str = service
        self.key: str = key
        self.shared: bool = shared
        self._codec: Any = codec

    @property
    def vm(self) -> PyExplicitRecoveryVM | None:
        return self.driver.vm if self._codec is None else None

    def get(self, key: str) -> Awaitable[bytes | None]:
        handle = self.driver.act(lambda vm: vm.get_state(key, key))
        read = self.driver.store_future(handle)
        codec = self._codec
        if codec is None:
            return read

        async def decoded() -> bytes | None:
            value = await read
            return None if value is None else typing.cast(bytes, await codec.decode(value))

        return decoded()

    def set(self, key: str, value: bytes) -> None:
        if self._codec is not None:
            value = self._codec.encode(value)
        self.driver.act(lambda vm: vm.set_state(key, value, key))

    def clear(self, key: str) -> None:
        self.driver.act(lambda vm: vm.clear_state(key, key))

    def state_keys(self) -> Awaitable[list[str]]:
        # Named under the state format's prefix, which tooling hides.
        handle = self.driver.act(lambda vm: vm.get_state_keys("v22/keys"))
        return self.driver.store_future(handle)

    def sync(self) -> Awaitable[Any]:
        """Clear ``SYNC_KEY`` with an ack: once acked, every earlier write is stored too."""
        handle = self.driver.act(lambda vm: vm.clear_state(SYNC_KEY, SYNC_KEY, True))
        return self.driver.store_future(handle)

    def take_cancellation(self) -> bool:
        """Whether a cancellation is owed to the handler's next await, taking it (see ``Attempt.asked``)."""
        return self.driver.take_carried_cancellation()

    def attempt_end(self) -> BaseException | None:
        return self.driver.ended

    def on_attempt_end(self, listener: Callable[[], None]) -> Callable[[], None]:
        return self.driver.on_end(listener)

    def cancel_invocation(self, invocation_id: str) -> None:
        self.driver.act(lambda vm: vm.cancel_invocation(invocation_id))

    def generic_call(
        self,
        service: str,
        handler: str,
        arg: bytes,
        key: str | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
        name: str | None = None,
    ) -> RestateDurableCallFuture[bytes]:
        handle = self.driver.act(
            lambda vm: vm.call(service, handler, arg, key, idempotency_key, _headers_of(headers), name)
        )
        call = _CallFuture(
            self.driver,
            self.driver.invocation_id_future(handle.invocation_id_handle),
            self.driver.future(handle.result_handle),
        )
        self.driver.track(call, handle.result_handle)
        return call

    def generic_send(
        self,
        service: str,
        handler: str,
        arg: bytes,
        key: str | None = None,
        send_delay: timedelta | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
        name: str | None = None,
    ) -> _SendHandle:
        delay = int(send_delay.total_seconds() * 1000) if send_delay is not None else None
        handle = self.driver.act(
            lambda vm: vm.send(service, handler, arg, key, delay, idempotency_key, _headers_of(headers), name)
        )
        return _SendHandle(self.driver, self.driver.invocation_id_future(handle))

    def attach_invocation(self, invocation_id: str) -> Awaitable[bytes]:
        return self.driver.future(self.driver.act(lambda vm: vm.attach_invocation(invocation_id)))

    def working(self) -> contextlib.AbstractAsyncContextManager[None]:
        """The handler's ``ctx.working``: the attempt is kept alive while its body runs."""
        return self.driver.working()

    def database_working(self) -> contextlib.AbstractAsyncContextManager[None]:
        """The same, around each call of the database (see ``_Boundary.alive``)."""
        return self.driver.working()

    def open_annotation(self, name: str, parent: int | None = None) -> int | None:
        return self.driver.act(lambda vm: vm.start_trace(name, parent))

    def close_annotation(self, handle: int, value: bytes, failure: str | None) -> None:
        try:
            if failure is None:
                self.driver.act(lambda vm: vm.end_trace_success(handle, value))
            else:
                self.driver.act(lambda vm: vm.end_trace_failure(handle, PyFailure(500, failure)))
        except SdkInternalBaseException:
            # The attempt ended: the next one fails the trace.
            pass
