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
"""A single loop that drives every pending wait of an invocation without journal replay.

Without journal replay, a handler's futures are plain awaitables that user
code awaits and combines however it likes: with ``asyncio.gather``,
``asyncio.wait``, its own tasks, or a library's. The protocol, however, has
one await point per invocation: a suspension, the awaiting-on hint and a
cancellation each take a single unresolved future. So every wait registers
with the selector, which asks the VM for progress on whichever of them
completes first, and which is the only reader of the invocation's input.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol

from restate.exceptions import SdkInternalException, SuspendedException, TerminalError
from restate.server_context import DisconnectedException, ServerInvocationContext
from restate.vm import (
    DoProgressAnyCompleted,
    DoProgressCancelSignalReceived,
    DoProgressExecuteRun,
    DoProgressWaitExternalProgress,
    FirstCompletedUnresolvedFuture,
    SingleUnresolvedFuture,
    Suspended,
    UnresolvedFuture,
)

logger = logging.getLogger(__name__)


class SelectorDriver(Protocol):
    """The operations of the invocation that the selector uses."""

    def do_progress(self, future: UnresolvedFuture) -> Any:
        """Return the VM's progress on ``future``: a DoProgress* response, ``Suspended``, or an exception."""
        ...

    def is_completed(self, handle: int) -> bool:
        """Return whether the VM has the result of ``handle``."""
        ...

    async def flush_output(self) -> None:
        """Write out whatever the VM has buffered."""
        ...

    async def receive(self) -> Mapping[str, Any]:
        """Return the next input event: a chunk of the request body, a disconnect, or a ``restate.*`` event."""
        ...

    def wake(self) -> None:
        """Make ``receive`` return a ``restate.*`` event."""
        ...

    def execute_run(self, handle: int) -> None:
        """Start the run of ``handle``, which signals its completion through ``receive``."""
        ...

    def notify_input(self, chunk: Mapping[str, Any]) -> None:
        """Pass a chunk of the request body to the VM."""
        ...

    def track(self, task: asyncio.Future[Any]) -> None:
        """Cancel ``task`` when the attempt ends."""
        ...

    def on_end(self) -> None:
        """Handle the end of the attempt, after the selector has failed every wait."""
        ...


def is_ready(future: UnresolvedFuture, completed: Callable[[int], bool]) -> bool:
    """Return whether ``future`` needs no more progress."""
    if isinstance(future, SingleUnresolvedFuture):
        return completed(future.handle)
    if isinstance(future, FirstCompletedUnresolvedFuture):
        return any(is_ready(child, completed) for child in future.children)
    return all(is_ready(child, completed) for child in future.children)


class FutureSelector:
    """The single loop that drives the waits of an invocation without journal replay.

    A wait stays registered until it is ready. Each pass, the loop resolves
    the waits that are ready, then asks the VM for progress on whichever
    pending wait completes first. So a suspension waits for any of them, a
    pending run starts whichever wait reaches it first, and a cancellation
    reaches all of them. Registering a wait while the loop reads input wakes
    the loop, so the new wait is included in the next check. The loop runs as
    long as something is pending.
    """

    def __init__(self, driver: SelectorDriver, disconnected: Callable[[], BaseException]) -> None:
        self._driver: SelectorDriver = driver
        self._disconnected: Callable[[], BaseException] = disconnected
        self._waits: dict[asyncio.Future[None], UnresolvedFuture] = {}
        self._task: asyncio.Task[None] | None = None
        # True between the VM's WaitExternalProgress and the next input.
        self._waiting: bool = False
        # Why the attempt ended, once it has; each later wait raises a fresh instance.
        self._end: Callable[[], BaseException] | None = None

    @property
    def ended(self) -> BaseException | None:
        """Why the attempt ended (a suspension, an error, a disconnect), or ``None`` while it continues."""
        return None if self._end is None else self._end()

    @property
    def size(self) -> int:
        """The number of waits still pending, for tests."""
        return len(self._waits)

    async def wait(self, future: UnresolvedFuture) -> None:
        """Wait until ``future`` is ready; raise if the attempt ends first or is cancelled."""
        if self._end is not None:
            raise self._end()
        if is_ready(future, self._driver.is_completed):
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waits[waiter] = future
        self._start()
        try:
            await waiter
        finally:
            self._waits.pop(waiter, None)

    def _start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            self._driver.track(self._task)
        elif self._waiting:
            # Only a loop that waits for input needs waking, and only once; otherwise
            # the loop picks up the new wait on its next pass.
            self._waiting = False
            self._driver.wake()

    def _settle(self) -> None:
        for waiter, future in self._waits.items():
            if not waiter.done() and is_ready(future, self._driver.is_completed):
                waiter.set_result(None)

    def _fail_all(self, error: Callable[[], BaseException]) -> None:
        for waiter in list(self._waits):
            if not waiter.done():
                waiter.set_exception(error())

    def _finish(self, end: Callable[[], BaseException]) -> None:
        """End the attempt: every wait, current or future, raises ``end()``."""
        self._end = end
        self._fail_all(end)
        self._driver.on_end()

    async def _loop(self) -> None:
        driver = self._driver
        try:
            while True:
                self._settle()
                # Let the code that was just resolved run and register its next
                # waits, so that one check covers them. This is only an
                # optimization: a later registration wakes the loop anyway.
                await asyncio.sleep(0)
                pending = [future for waiter, future in self._waits.items() if not waiter.done()]
                if not pending:
                    return
                await driver.flush_output()
                response = driver.do_progress(
                    pending[0] if len(pending) == 1 else FirstCompletedUnresolvedFuture(pending)
                )
                if isinstance(response, BaseException):
                    logger.exception("Exception in do_progress", exc_info=response)
                    self._finish(_internal(response))
                    return
                if isinstance(response, Suspended):
                    self._finish(SuspendedException)
                    return
                if isinstance(response, DoProgressAnyCompleted):
                    continue
                if isinstance(response, DoProgressCancelSignalReceived):
                    # Cancel every pending wait; waits registered afterwards are not cancelled.
                    self._fail_all(lambda: TerminalError("cancelled", 409))
                    continue
                if isinstance(response, DoProgressExecuteRun):
                    driver.execute_run(response.handle)
                    continue
                if isinstance(response, DoProgressWaitExternalProgress):
                    self._waiting = True
                    await driver.flush_output()
                    chunk = await driver.receive()
                    self._waiting = False
                    kind = chunk.get("type")
                    if kind == "http.disconnect":
                        self._finish(self._disconnected)
                        return
                    if kind == "http.request":
                        driver.notify_input(chunk)
        except asyncio.CancelledError:
            # The attempt is over.
            self._end = self._end or SuspendedException
            self._fail_all(self._end)
            raise
        except BaseException as error:  # pylint: disable=broad-exception-caught
            logger.exception("Exception in the invocation's selector")
            self._finish(_internal(error))
        finally:
            self._task = None


def _internal(cause: BaseException) -> Callable[[], BaseException]:
    """Return a factory of ``SdkInternalException`` instances caused by ``cause``."""

    def internal() -> BaseException:
        error = SdkInternalException()
        error.__cause__ = cause
        return error

    return internal


# ``hook`` makes the selector take over an SDK invocation: the waits of its
# futures, which the SDK makes through ``create_poll_or_cancel_coroutine``, go
# to the selector, and each future starts as soon as it is created. This
# reaches into ``ServerInvocationContext``, from the same package, whose tests
# cover it.
# TODO: once sessions run on it, the context could drive its waits through
# the selector itself, instead of being hooked.


class _ContextDriver:
    """A ``SelectorDriver`` over the SDK's invocation context."""

    def __init__(self, context: ServerInvocationContext, on_end: Callable[[], None]) -> None:
        self.context: ServerInvocationContext = context
        self.on_end: Callable[[], None] = on_end

    def do_progress(self, future: UnresolvedFuture) -> Any:
        return self.context.vm.do_progress(future)

    def is_completed(self, handle: int) -> bool:
        return self.context.vm.is_completed(handle)

    async def flush_output(self) -> None:
        await self.context.take_and_send_output()

    async def receive(self) -> Mapping[str, Any]:
        return await self.context.receive()

    def wake(self) -> None:
        # Any restate.* event will do: the loop treats it as a wake-up.
        event = self.context.receive.enqueue_restate_event({"type": "restate.run_completed", "data": None})
        self.track(asyncio.ensure_future(event))

    def execute_run(self, handle: int) -> None:
        context = self.context
        fn = context.run_coros_to_execute.pop(handle)

        async def run() -> None:
            try:
                await fn()
            finally:
                await context.receive.enqueue_restate_event({"type": "restate.run_completed", "data": None})

        self.track(asyncio.create_task(run()))

    def notify_input(self, chunk: Mapping[str, Any]) -> None:
        # Skip empty body frames, as the SDK does.
        body = chunk.get("body")
        if body is not None and len(body) > 0:
            self.context.vm.notify_input(body)
        if not chunk.get("more_body", False):
            self.context.vm.notify_input_closed()

    def track(self, task: asyncio.Future[Any]) -> None:
        self.context.tasks.add(task)


def hook(context: ServerInvocationContext, on_end: Callable[[], None]) -> FutureSelector:
    """Route the waits of ``context``, an invocation without journal replay, through one selector.

    Each future starts as soon as it is created, whether or not anything
    awaits it. The selector calls ``on_end`` if it has to fail every wait.
    The hook lasts as long as ``context``, which is one attempt.
    """
    selector = FutureSelector(_ContextDriver(context, on_end), DisconnectedException)
    started: set[asyncio.Task[Any]] = set()

    def starting(make: Callable[..., Any]) -> Callable[..., Any]:
        def make_started(*args: Any, **kwargs: Any) -> Any:
            future = make(*args, **kwargs)
            lazy = future.future
            if lazy.task is None:
                lazy.task = asyncio.create_task(lazy.async_def())
                # If nothing awaits it, its error is dropped.
                lazy.task.add_done_callback(_retrieve)
                started.add(lazy.task)
                lazy.task.add_done_callback(started.discard)
            return future

        return make_started

    for name in ("create_future", "create_sleep_future", "create_call_future"):
        setattr(context, name, starting(getattr(context, name)))
    context.create_poll_or_cancel_coroutine = selector.wait  # type: ignore[method-assign,assignment]  # pyright: ignore[reportAttributeAccessIssue]
    return selector


def _retrieve(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()
