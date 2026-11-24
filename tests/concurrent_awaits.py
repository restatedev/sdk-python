#
#  Copyright (c) 2023-2025 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""
Regression tests for #315: futures awaited from several asyncio tasks at once.

Each awaiting task polls the VM for its own handle. An input event read by one task can complete
another task's handle, so every poller must poll again once the event reaches the VM.
"""

import asyncio
from typing import Any, cast

import pytest

from restate.server_context import DisconnectedException, ServerInvocationContext
from restate.server_types import ASGIReceiveEvent, ReceiveChannel, RestateEvent
from restate.vm import (
    DO_PROGRESS_ANY_COMPLETED,
    DO_PROGRESS_WAIT_EXTERNAL_PROGRESS,
    SingleUnresolvedFuture,
    UnresolvedFuture,
)


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [
    pytest.mark.anyio,
]


class FakeVM:
    """Completes the handles listed in each input body, such as b"0,2"."""

    def __init__(self) -> None:
        self.completed: set[int] = set()

    def do_progress(self, unresolved_future: UnresolvedFuture) -> Any:
        assert isinstance(unresolved_future, SingleUnresolvedFuture)
        if unresolved_future.handle in self.completed:
            return DO_PROGRESS_ANY_COMPLETED
        return DO_PROGRESS_WAIT_EXTERNAL_PROGRESS

    def notify_input(self, body: bytes) -> None:
        self.completed.update(int(handle) for handle in body.split(b","))

    def notify_input_closed(self) -> None:
        pass

    def take_output(self) -> None:
        return None


class Harness:
    """A ServerInvocationContext wired to a FakeVM and to input events the test sends."""

    def __init__(self) -> None:
        self.events: asyncio.Queue[ASGIReceiveEvent] = asyncio.Queue()
        self.receive = ReceiveChannel(self.events.get)
        self.vm = FakeVM()
        # Only the attributes the poll loop uses: the rest of the context is not needed here.
        context = ServerInvocationContext.__new__(ServerInvocationContext)
        context.vm = cast(Any, self.vm)
        context.receive = self.receive
        context._output_lock = asyncio.Lock()  # pylint: disable=protected-access
        context._input_reader_active = False  # pylint: disable=protected-access
        context._input_handled = asyncio.Event()  # pylint: disable=protected-access
        self.context = context

    def poll(self, handle: int) -> asyncio.Task[None]:
        return asyncio.create_task(self.context.create_poll_or_cancel_coroutine(SingleUnresolvedFuture(handle)))

    async def send_input(self, body: bytes) -> None:
        await self.events.put({"type": "http.request", "body": body, "more_body": True})

    async def send_restate_event(self, event: RestateEvent) -> None:
        await self.receive.enqueue_restate_event(event)

    async def close(self) -> None:
        await self.receive.close()


async def settle() -> None:
    """Let every task run until it blocks."""
    for _ in range(10):
        await asyncio.sleep(0)


async def test_an_event_completing_several_handles_wakes_every_poller():
    harness = Harness()
    pollers = [harness.poll(handle) for handle in range(3)]
    await settle()

    await harness.send_input(b"0,1,2")

    async with asyncio.timeout(1):
        await asyncio.gather(*pollers)
    await harness.close()


async def test_a_poller_whose_handle_another_poller_read_completes():
    harness = Harness()
    first, second = harness.poll(0), harness.poll(1)
    await settle()

    # Whichever poller reads this event, it completes handle 1 only.
    await harness.send_input(b"1")
    async with asyncio.timeout(1):
        await second
    assert not first.done()

    await harness.send_input(b"0")
    async with asyncio.timeout(1):
        await first
    await harness.close()


async def test_a_run_completion_wakes_every_poller():
    harness = Harness()
    pollers = [harness.poll(handle) for handle in range(2)]
    await settle()

    # A run's result reaches the VM before its completion event is read.
    harness.vm.completed.update({0, 1})
    await harness.send_restate_event({"type": "restate.run_completed", "data": None})

    async with asyncio.timeout(1):
        await asyncio.gather(*pollers)
    await harness.close()


async def test_another_poller_reads_once_the_reader_is_cancelled():
    harness = Harness()
    first, second = harness.poll(0), harness.poll(1)
    await settle()

    first.cancel()
    await settle()
    await harness.send_input(b"1")

    async with asyncio.timeout(1):
        await second
    assert first.cancelled()
    await harness.close()


async def test_a_disconnect_reaches_every_poller():
    harness = Harness()
    pollers = [harness.poll(handle) for handle in range(2)]
    await settle()

    await harness.events.put({"type": "http.disconnect"})

    async with asyncio.timeout(1):
        results = await asyncio.gather(*pollers, return_exceptions=True)
    assert all(isinstance(result, DisconnectedException) for result in results)
    await harness.close()
