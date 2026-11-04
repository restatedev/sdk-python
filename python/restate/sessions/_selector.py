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
One loop that drives every pending wait of an invocation without journal replay.

Without journal replay, the handler's futures are plain awaitables that user
code awaits and combines as it likes: ``asyncio.gather``, ``asyncio.wait``,
its own tasks, or a library's. The protocol has one await point per
invocation, though: a suspension, the awaiting-on hint and a cancellation each
take one unresolved future. So every wait is registered with the selector,
which asks the VM for progress on the first of all of them to complete, and is
the only one reading the invocation's input.
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
    """What the selector needs of the invocation."""

    def do_progress(self, future: UnresolvedFuture) -> Any:
        """The VM's progress on ``future``: a DoProgress* response, Suspended, or an exception."""
        ...

    def is_completed(self, handle: int) -> bool:
        """Whether the VM has the result of ``handle``."""
        ...

    async def flush_output(self) -> None:
        """Writes out what the VM has buffered."""
        ...

    async def receive(self) -> Mapping[str, Any]:
        """The next input event: a chunk of the request body, a disconnect, or a restate.* event."""
        ...

    def wake(self) -> None:
        """Makes ``receive`` return a restate.* event."""
        ...

    def execute_run(self, handle: int) -> None:
        """Starts the run of ``handle``, which signals its completion through ``receive``."""
        ...

    def notify_input(self, chunk: Mapping[str, Any]) -> None:
        """Passes a chunk of the request body to the VM."""
        ...

    def track(self, task: asyncio.Future[Any]) -> None:
        """Cancels ``task`` at the end of the attempt."""
        ...

    def on_end(self) -> None:
        """The attempt cannot go on: the selector has failed every wait."""
        ...


def is_ready(future: UnresolvedFuture, completed: Callable[[int], bool]) -> bool:
    """Whether ``future`` needs no more progress."""
    if isinstance(future, SingleUnresolvedFuture):
        return completed(future.handle)
    if isinstance(future, FirstCompletedUnresolvedFuture):
        return any(is_ready(child, completed) for child in future.children)
    return all(is_ready(child, completed) for child in future.children)


class FutureSelector:
    """
    The one loop of an invocation without journal replay.

    A wait stays registered until it is ready. The loop settles whatever is
    ready, then asks the VM for progress on the first of all the pending waits
    to complete: so a suspension waits for any of them, a pending run starts
    whichever wait reaches it first, and a cancellation reaches all of them. A
    wait registered while the loop reads the input wakes it, so it is part of
    the next check. The loop runs while something is pending.
    """

    def __init__(self, driver: SelectorDriver, disconnected: Callable[[], BaseException]) -> None:
        self._driver: SelectorDriver = driver
        self._disconnected: Callable[[], BaseException] = disconnected
        self._waits: dict[asyncio.Future[None], UnresolvedFuture] = {}
        self._task: asyncio.Task[None] | None = None
        # Between the VM's WaitExternalProgress and the next input.
        self._waiting: bool = False
        # Why the attempt ended, once it has: each wait then raises a new one.
        self._end: Callable[[], BaseException] | None = None

    @property
    def ended(self) -> BaseException | None:
        """Why the attempt ended (a suspension, an error, a disconnect), or None while it goes on."""
        return None if self._end is None else self._end()

    @property
    def size(self) -> int:
        """The waits still pending, for tests."""
        return len(self._waits)

    async def wait(self, future: UnresolvedFuture) -> None:
        """Returns once ``future`` is ready; raises if the attempt ends first, or is cancelled."""
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
            # Only a loop waiting for input needs waking, once: any other picks
            # up the new wait on its next pass.
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
        """The attempt cannot go on: every wait, now and later, raises ``end()``."""
        self._end = end
        self._fail_all(end)
        self._driver.on_end()

    async def _loop(self) -> None:
        driver = self._driver
        try:
            while True:
                self._settle()
                # Let what that settled run, and register what it waits for
                # next, so that one check covers them. An optimization: what
                # registers later wakes the loop anyway.
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
                    # Reaches every pending wait; what waits after it is not cancelled.
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
    """New SdkInternalExceptions, caused by ``cause``."""

    def internal() -> BaseException:
        error = SdkInternalException()
        error.__cause__ = cause
        return error

    return internal


# The selector takes over an invocation of the SDK's (``hook``): the waits of
# its futures, which the SDK makes through ``create_poll_or_cancel_coroutine``,
# go to the selector, and each future starts when it is made. It reaches into
# ``ServerInvocationContext``, of this same package, whose tests cover it.
# TODO: once sessions run on it, the context could drive its waits through
# the selector itself, rather than be hooked.


class _ContextDriver:
    """What the selector needs of the SDK's context."""

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
        # Any restate.* event: the loop takes it as a wake-up.
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
    """
    Make the waits of ``context``, an invocation without journal replay, go
    through one selector, which calls ``on_end`` if it fails them all; each
    future starts when it is made, whether or not anything awaits it. The
    hook lasts as long as ``context``, one attempt.
    """
    selector = FutureSelector(_ContextDriver(context, on_end), DisconnectedException)
    started: set[asyncio.Task[Any]] = set()

    def starting(make: Callable[..., Any]) -> Callable[..., Any]:
        def make_started(*args: Any, **kwargs: Any) -> Any:
            future = make(*args, **kwargs)
            lazy = future.future
            if lazy.task is None:
                lazy.task = asyncio.create_task(lazy.async_def())
                # Its error, if nothing awaits it, is dropped.
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
