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
"""The selector of an invocation without journal replay, against a fake VM."""

# pylint: disable=missing-function-docstring

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from restate.exceptions import SdkInternalException, SuspendedException, TerminalError
from restate.vm import (
    CANCEL_HANDLE,
    AllCompletedUnresolvedFuture,
    DoProgressAnyCompleted,
    DoProgressCancelSignalReceived,
    DoProgressExecuteRun,
    DoProgressWaitExternalProgress,
    FirstCompletedUnresolvedFuture,
    SingleUnresolvedFuture,
    Suspended,
    UnresolvedFuture,
)
from restate.sessions._selector import FutureSelector, is_ready


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]


class Disconnected(Exception):
    pass


class FakeVm:
    """
    A VM with the shared core's rules for ``do_progress``, as far as the
    selector sees them (sdk-shared-core, ``src/vm/transitions/async_results.rs``).

    A body chunk b"3" brings in the result of handle 3, which a check takes in
    (``completed``) only while its future is not resolved. A check whose
    future is resolved returns ``AnyCompleted``; else it hands out a run of the
    future that has not started (``ExecuteRun``); else, with the input closed
    and no run of the future executing, it suspends the invocation on the
    future; else it waits.
    """

    def __init__(self) -> None:
        self.completed: set[int] = set()
        # What came in and no check took in yet, in order.
        self.arrived: list[int] = []
        self.input: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.checks: list[UnresolvedFuture] = []
        # The checks of the cancellation alone: each would tell Restate that the handler waits for it, and
        # suspend the invocation on it once the input closed.
        self.alone: list[UnresolvedFuture] = []
        # Responses that do_progress gives before looking at the handles.
        self.script: list[Any] = []
        # Runs not yet handed out, and those executing.
        self.to_execute: set[int] = set()
        self.executing: set[int] = set()
        self.input_closed = False
        self.suspended_on: UnresolvedFuture | None = None
        self.runs: list[int] = []
        self.work_handle: int | None = None
        self.work_ended: list[int] = []
        self.wakes = 0
        self.ended = 0
        self.tasks: list[asyncio.Future] = []

    def do_progress(self, future: UnresolvedFuture) -> Any:
        if self.suspended_on is not None:
            return ValueError("closed")
        self.checks.append(future)
        if all(handle == CANCEL_HANDLE for handle in leaves(future)):
            self.alone.append(future)
        if self.script:
            return self.script.pop(0)
        while not is_ready(future, self.is_completed) and self.arrived:
            self.completed.add(self.arrived.pop(0))
        if is_ready(future, self.is_completed):
            return DoProgressAnyCompleted()
        for handle in leaves(future):
            if handle in self.to_execute:
                self.to_execute.discard(handle)
                self.executing.add(handle)
                return DoProgressExecuteRun(handle)
        if self.input_closed and not any(handle in self.executing for handle in leaves(future)):
            self.suspended_on = future
            return Suspended()
        return DoProgressWaitExternalProgress()

    def is_completed(self, handle: int) -> bool:
        return handle in self.completed

    async def flush_output(self) -> None:
        pass

    async def receive(self) -> dict[str, Any]:
        return await self.input.get()

    def wake(self) -> None:
        self.wakes += 1
        self.input.put_nowait({"type": "restate.wake"})

    def execute_run(self, handle: int) -> None:
        self.runs.append(handle)
        if handle == self.work_handle:
            return
        self.executing.discard(handle)
        self.completed.add(handle)
        self.input.put_nowait({"type": "restate.run_completed"})

    def work(self) -> int:
        if self.work_handle is None:
            self.work_handle = WORK
            self.to_execute.add(WORK)
        return self.work_handle

    def end_work(self) -> None:
        if self.work_handle is not None:
            self.work_ended.append(self.work_handle)
            self.work_handle = None

    def notify_input(self, chunk: Mapping[str, Any]) -> None:
        if chunk.get("body"):
            self.arrived.append(int(chunk["body"]))
        if not chunk.get("more_body", False):
            self.input_closed = True

    def track(self, task: asyncio.Future) -> None:
        self.tasks.append(task)

    def on_end(self) -> None:
        self.ended += 1

    def complete(self, handle: int) -> None:
        self.input.put_nowait({"type": "http.request", "body": str(handle).encode(), "more_body": True})

    def close_input(self, handle: int | None = None) -> None:
        """Close the input, with the result of ``handle`` in its last chunk if given."""
        body = b"" if handle is None else str(handle).encode()
        self.input.put_nowait({"type": "http.request", "body": body, "more_body": False})


WORK = 9999
"""The handle the fake VM gives the working run."""


def leaves(future: UnresolvedFuture) -> list[int]:
    if isinstance(future, SingleUnresolvedFuture):
        return [future.handle]
    return [handle for child in future.children for handle in leaves(child)]


def single(handle: int) -> SingleUnresolvedFuture:
    return SingleUnresolvedFuture(handle)


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_a_ready_future_needs_no_loop():
    vm = FakeVm()
    vm.completed.add(11)
    selector = FutureSelector(vm, Disconnected)
    await selector.wait(single(11))
    assert vm.checks == []


async def test_concurrent_waits_share_one_await_point():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    both = asyncio.ensure_future(asyncio.gather(selector.wait(single(11)), selector.wait(single(12))))
    await settle()
    # One check, of the first of both to complete.
    assert vm.checks == [FirstCompletedUnresolvedFuture([single(11), single(12)])]
    vm.complete(12)
    await settle()
    assert not both.done()
    vm.complete(11)
    await both
    assert selector.size == 0


async def test_a_wait_made_while_the_loop_waits_for_input_joins_the_next_check():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    first = asyncio.ensure_future(selector.wait(single(11)))
    await settle()
    assert vm.checks == [single(11)]
    second = asyncio.ensure_future(selector.wait(single(12)))
    await settle()
    assert vm.wakes == 1
    assert vm.checks[-1] == FirstCompletedUnresolvedFuture([single(11), single(12)])
    # A third wait while the loop waits again wakes it once more, not once per wait.
    third = asyncio.ensure_future(selector.wait(single(13)))
    await settle()
    assert vm.wakes == 2
    for handle in (13, 12, 11):
        vm.complete(handle)
    await asyncio.gather(first, second, third)


async def test_a_wait_made_while_the_loop_flushes_is_checked_before_it_waits_for_input():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    late: list[asyncio.Future] = []

    async def flush_letting_a_wait_register() -> None:
        if not late:
            late.append(asyncio.ensure_future(selector.wait(single(12))))
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    vm.flush_output = flush_letting_a_wait_register  # type: ignore[method-assign]
    first = asyncio.ensure_future(selector.wait(single(11)))
    await settle()
    await settle()
    # Not left out of the check the loop was making while it registered, nor waiting for input or a wake.
    assert vm.checks[-1] == FirstCompletedUnresolvedFuture([single(11), single(12)])
    vm.complete(12)
    vm.complete(11)
    await asyncio.gather(first, *late)


async def test_combinators_wait_for_their_children():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    every = asyncio.ensure_future(selector.wait(AllCompletedUnresolvedFuture([single(11), single(12)])))
    vm.complete(11)
    await settle()
    assert not every.done()
    vm.complete(12)
    await every


async def test_a_cancellation_reaches_every_pending_wait_and_only_those():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    waits = [asyncio.ensure_future(selector.wait(single(h))) for h in (11, 12)]
    await settle()
    vm.script.append(DoProgressCancelSignalReceived())
    vm.complete(9)
    for wait in waits:
        with pytest.raises(TerminalError) as error:
            await wait
        assert error.value.status_code == 409
    # What waits after it is not cancelled.
    later = asyncio.ensure_future(selector.wait(single(13)))
    vm.complete(13)
    await later


async def test_a_suspension_ends_every_wait_now_and_later():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    vm.script.append(Suspended())
    with pytest.raises(SuspendedException):
        await selector.wait(single(11))
    assert isinstance(selector.ended, SuspendedException)
    assert vm.ended == 1
    with pytest.raises(SuspendedException):
        await selector.wait(single(12))


async def test_an_error_of_the_vm_ends_the_attempt():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    vm.script.append(ValueError("journal mismatch"))
    with pytest.raises(SdkInternalException) as error:
        await selector.wait(single(11))
    assert isinstance(error.value.__cause__, ValueError)


async def test_a_disconnect_ends_the_attempt():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    wait = asyncio.ensure_future(selector.wait(single(11)))
    await settle()
    vm.input.put_nowait({"type": "http.disconnect"})
    with pytest.raises(Disconnected):
        await wait


async def test_a_run_is_started_and_its_completion_wakes_the_loop():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    vm.script.append(DoProgressExecuteRun(7))
    await selector.wait(single(7))
    assert vm.runs == [7]


async def test_the_loop_stops_when_nothing_is_pending_and_starts_again():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    first = asyncio.ensure_future(selector.wait(single(11)))
    vm.complete(11)
    await first
    await settle()
    assert all(task.done() for task in vm.tasks)
    second = asyncio.ensure_future(selector.wait(single(12)))
    vm.complete(12)
    await second
    assert len(vm.tasks) == 2


# --- The cancellation awaited alone, as a handler that works outside Restate awaits it -------------------------------

CANCELLATION = single(CANCEL_HANDLE)
WATCHING = FirstCompletedUnresolvedFuture([CANCELLATION, single(WORK)])
"""A check of the cancellation alone: with the working run, which stands for the handler's work."""


async def test_the_cancellation_alone_is_asked_for_with_the_working_run_and_settles_when_it_comes():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    cancelled = asyncio.ensure_future(selector.wait(CANCELLATION))
    await settle()
    # The first check starts the working run, the second waits: never for the cancellation alone.
    assert vm.checks == [WATCHING, WATCHING]
    assert vm.runs == [WORK]
    assert vm.alone == []
    vm.complete(CANCEL_HANDLE)
    await cancelled
    assert selector.ended is None


async def test_the_cancellation_alone_does_not_suspend_once_the_input_closed():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    cancelled = asyncio.ensure_future(selector.wait(CANCELLATION))
    await settle()
    vm.close_input()
    await settle()
    assert vm.suspended_on is None
    assert selector.ended is None
    assert vm.alone == []
    assert not cancelled.done()


async def test_the_cancellation_alone_does_not_suspend_if_the_input_closed_before_it_was_awaited():
    vm = FakeVm()
    vm.input_closed = True
    selector = FutureSelector(vm, Disconnected)
    cancelled = asyncio.ensure_future(selector.wait(CANCELLATION))
    await settle()
    assert vm.suspended_on is None
    assert selector.ended is None
    assert vm.runs == [WORK]
    assert vm.alone == []
    assert not cancelled.done()


async def test_what_is_awaited_after_the_input_closed_suspends_with_the_cancellation_without_the_working_run():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    cancelled = asyncio.ensure_future(selector.wait(CANCELLATION))
    await settle()
    vm.close_input()
    await settle()
    with pytest.raises(SuspendedException):
        await selector.wait(single(20))
    assert vm.suspended_on == FirstCompletedUnresolvedFuture([CANCELLATION, single(20)])
    with pytest.raises(SuspendedException):
        await cancelled


async def test_a_run_awaited_after_the_input_closed_settles_in_the_same_attempt():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    cancelled = asyncio.ensure_future(selector.wait(CANCELLATION))
    await settle()
    vm.close_input()
    await settle()
    vm.to_execute.add(21)
    await selector.wait(single(21))
    await settle()
    assert vm.runs == [WORK, 21]
    assert vm.suspended_on is None
    assert selector.ended is None
    assert not cancelled.done()


async def test_a_cancel_signal_that_came_as_the_input_closed_settles_the_cancellation_at_once():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    cancelled = asyncio.ensure_future(selector.wait(CANCELLATION))
    await settle()
    vm.close_input(CANCEL_HANDLE)
    await cancelled
    assert vm.suspended_on is None
    assert selector.ended is None


async def test_a_combinator_over_the_cancellation_and_something_else_suspends_on_both():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    either = FirstCompletedUnresolvedFuture([CANCELLATION, single(40)])
    wait = asyncio.ensure_future(selector.wait(either))
    await settle()
    vm.close_input()
    with pytest.raises(SuspendedException):
        await wait
    assert vm.suspended_on == either
    assert vm.work_handle is None


async def test_close_fails_the_watch_ends_the_working_run_and_stops_the_loop():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    cancelled = asyncio.ensure_future(selector.wait(CANCELLATION))
    await settle()
    selector.close()
    with pytest.raises(SuspendedException):
        await cancelled
    assert vm.work_ended == [WORK]
    await settle()
    assert all(task.done() for task in vm.tasks)
    # A handler that never watched has no working run to end.
    other = FakeVm()
    FutureSelector(other, Disconnected).close()
    assert other.work_ended == []


async def test_the_cancellation_is_never_asked_for_alone_whatever_else_comes_and_goes():
    seed = 11

    def random() -> float:
        nonlocal seed
        seed = (seed * 1103515245 + 12345) % 2**31
        return seed / 2**31

    suspended = worked = 0
    for round_ in range(150):
        vm = FakeVm()
        selector = FutureSelector(vm, Disconnected)
        waits: dict[int, asyncio.Future] = {}
        close_at = int(random() * 60)
        watch_at = int(random() * 30)
        handle = 100
        for step in range(60):
            if vm.suspended_on is not None:
                break
            if step == watch_at:
                waits[CANCEL_HANDLE] = asyncio.ensure_future(selector.wait(CANCELLATION))
            if step == close_at:
                vm.close_input()
            x = random()
            if x < 0.25:
                handle += 1
                # Every other round, the handler awaits runs only: with the input closed, anything else suspends it.
                if round_ % 2 == 0 or random() < 0.5:
                    vm.to_execute.add(handle)
                waits[handle] = asyncio.ensure_future(selector.wait(single(handle)))
            elif x < 0.5:
                # Once the input closed, nothing comes in.
                open_ = [h for h in waits if h not in (CANCEL_HANDLE,) and h not in vm.to_execute]
                if open_ and not vm.input_closed:
                    vm.complete(open_[int(random() * len(open_))])
            if random() < 0.6:
                await asyncio.sleep(0)
        for _ in range(20):
            await asyncio.sleep(0)
        assert vm.alone == []
        on = vm.suspended_on
        # It suspends only on something else the handler waits for.
        assert on is None or any(h != CANCEL_HANDLE for h in leaves(on))
        if on is not None:
            suspended += 1
        elif vm.input_closed:
            worked += 1
        selector.close()
        await asyncio.gather(*waits.values(), return_exceptions=True)
    # Both outcomes came up.
    assert suspended > 10
    assert worked > 10
