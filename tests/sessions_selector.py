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
    A VM whose handles complete as input names them: a body chunk b"3" completes
    handle 3.
    """

    def __init__(self) -> None:
        self.completed: set[int] = set()
        self.input: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.checks: list[UnresolvedFuture] = []
        # Responses that do_progress gives before looking at the handles.
        self.script: list[Any] = []
        self.runs: list[int] = []
        self.wakes = 0
        self.ended = 0
        self.tasks: list[asyncio.Future] = []

    def do_progress(self, future: UnresolvedFuture) -> Any:
        self.checks.append(future)
        if self.script:
            return self.script.pop(0)
        if is_ready(future, self.is_completed):
            return DoProgressAnyCompleted()
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
        self.completed.add(handle)
        self.input.put_nowait({"type": "restate.run_completed"})

    def notify_input(self, chunk: Mapping[str, Any]) -> None:
        self.completed.add(int(chunk["body"]))

    def track(self, task: asyncio.Future) -> None:
        self.tasks.append(task)

    def on_end(self) -> None:
        self.ended += 1

    def complete(self, handle: int) -> None:
        self.input.put_nowait({"type": "http.request", "body": str(handle).encode(), "more_body": True})


def single(handle: int) -> SingleUnresolvedFuture:
    return SingleUnresolvedFuture(handle)


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_a_ready_future_needs_no_loop():
    vm = FakeVm()
    vm.completed.add(1)
    selector = FutureSelector(vm, Disconnected)
    await selector.wait(single(1))
    assert vm.checks == []


async def test_concurrent_waits_share_one_await_point():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    both = asyncio.ensure_future(asyncio.gather(selector.wait(single(1)), selector.wait(single(2))))
    await settle()
    # One check, of the first of both to complete.
    assert vm.checks == [FirstCompletedUnresolvedFuture([single(1), single(2)])]
    vm.complete(2)
    await settle()
    assert not both.done()
    vm.complete(1)
    await both
    assert selector.size == 0


async def test_a_wait_made_while_the_loop_waits_for_input_joins_the_next_check():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    first = asyncio.ensure_future(selector.wait(single(1)))
    await settle()
    assert vm.checks == [single(1)]
    second = asyncio.ensure_future(selector.wait(single(2)))
    await settle()
    assert vm.wakes == 1
    assert vm.checks[-1] == FirstCompletedUnresolvedFuture([single(1), single(2)])
    # A third wait while the loop waits again wakes it once more, not once per wait.
    third = asyncio.ensure_future(selector.wait(single(3)))
    await settle()
    assert vm.wakes == 2
    for handle in (3, 2, 1):
        vm.complete(handle)
    await asyncio.gather(first, second, third)


async def test_combinators_wait_for_their_children():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    every = asyncio.ensure_future(selector.wait(AllCompletedUnresolvedFuture([single(1), single(2)])))
    vm.complete(1)
    await settle()
    assert not every.done()
    vm.complete(2)
    await every


async def test_a_cancellation_reaches_every_pending_wait_and_only_those():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    waits = [asyncio.ensure_future(selector.wait(single(h))) for h in (1, 2)]
    await settle()
    vm.script.append(DoProgressCancelSignalReceived())
    vm.complete(9)
    for wait in waits:
        with pytest.raises(TerminalError) as error:
            await wait
        assert error.value.status_code == 409
    # What waits after it is not cancelled.
    later = asyncio.ensure_future(selector.wait(single(3)))
    vm.complete(3)
    await later


async def test_a_suspension_ends_every_wait_now_and_later():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    vm.script.append(Suspended())
    with pytest.raises(SuspendedException):
        await selector.wait(single(1))
    assert isinstance(selector.ended, SuspendedException)
    assert vm.ended == 1
    with pytest.raises(SuspendedException):
        await selector.wait(single(2))


async def test_an_error_of_the_vm_ends_the_attempt():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    vm.script.append(ValueError("journal mismatch"))
    with pytest.raises(SdkInternalException) as error:
        await selector.wait(single(1))
    assert isinstance(error.value.__cause__, ValueError)


async def test_a_disconnect_ends_the_attempt():
    vm = FakeVm()
    selector = FutureSelector(vm, Disconnected)
    wait = asyncio.ensure_future(selector.wait(single(1)))
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
    first = asyncio.ensure_future(selector.wait(single(1)))
    vm.complete(1)
    await first
    await settle()
    assert all(task.done() for task in vm.tasks)
    second = asyncio.ensure_future(selector.wait(single(2)))
    vm.complete(2)
    await second
    assert len(vm.tasks) == 2
