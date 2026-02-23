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
Regression tests for the SIGTERM hot-loop bug.

When a BidiStream disconnects (e.g. pod receives SIGTERM), two interacting bugs
caused the worker to spin at 82% CPU and never exit:

1. ReceiveChannel.__call__() blocked forever on an empty queue after the
   disconnect event had been consumed — no more events would ever arrive.
2. create_poll_or_cancel_coroutine() fed empty body frames (b'') to the VM
   via notify_input(), causing a synchronous tight loop.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from restate.server_types import ReceiveChannel


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [
    pytest.mark.anyio,
]


async def test_receive_channel_returns_disconnect_when_drained():
    """After disconnect, an empty queue should return http.disconnect immediately."""
    events = [
        {"type": "http.request", "body": b"hello", "more_body": True},
        {"type": "http.request", "body": b"", "more_body": False},
        {"type": "http.disconnect"},
    ]
    event_iter = iter(events)

    async def mock_receive():
        try:
            return next(event_iter)
        except StopIteration:
            # Block forever — simulates the real ASGI receive after disconnect
            await asyncio.Event().wait()

    channel = ReceiveChannel(mock_receive)

    # Drain all queued events
    try:
        result1 = await asyncio.wait_for(channel(), timeout=1.0)
        assert result1["type"] == "http.request"

        result2 = await asyncio.wait_for(channel(), timeout=1.0)
        assert result2["type"] == "http.request"

        result3 = await asyncio.wait_for(channel(), timeout=1.0)
        assert result3["type"] == "http.disconnect"

        # Now the queue is drained and _disconnected is set.
        # This call should return immediately with a synthetic disconnect,
        # NOT block forever.
        result4 = await asyncio.wait_for(channel(), timeout=1.0)
        assert result4["type"] == "http.disconnect"
    finally:
        await channel.close()


async def test_receive_channel_does_not_block_after_disconnect():
    """Repeated calls after disconnect should all return promptly."""
    events = [
        {"type": "http.disconnect"},
    ]
    event_iter = iter(events)

    async def mock_receive():
        try:
            return next(event_iter)
        except StopIteration:
            await asyncio.Event().wait()

    channel = ReceiveChannel(mock_receive)

    try:
        # Consume the real disconnect
        result = await asyncio.wait_for(channel(), timeout=1.0)
        assert result["type"] == "http.disconnect"

        # Subsequent calls should not block
        for _ in range(5):
            result = await asyncio.wait_for(channel(), timeout=0.5)
            assert result["type"] == "http.disconnect"
    finally:
        await channel.close()


async def test_empty_body_frames_do_not_cause_hotloop():
    """
    When the VM returns DoProgressReadFromInput and the chunk has body=b'',
    notify_input should NOT be called (it would cause a tight loop).
    The loop should exit via DisconnectedException when http.disconnect arrives.
    """
    from restate.server_context import ServerInvocationContext, DisconnectedException
    from restate.vm import DoProgressReadFromInput

    # Build a minimal mock context
    vm = MagicMock()
    vm.take_output.return_value = None
    vm.do_progress.return_value = DoProgressReadFromInput()

    handler = MagicMock()
    invocation = MagicMock()
    send = MagicMock()

    events = [
        {"type": "http.request", "body": b"", "more_body": True},
        {"type": "http.request", "body": b"", "more_body": False},
        {"type": "http.disconnect"},
    ]
    event_iter = iter(events)

    async def mock_receive():
        try:
            return next(event_iter)
        except StopIteration:
            await asyncio.Event().wait()

    receive_channel = ReceiveChannel(mock_receive)

    ctx = ServerInvocationContext.__new__(ServerInvocationContext)
    ctx.vm = vm
    ctx.handler = handler
    ctx.invocation = invocation
    ctx.send = send
    ctx.receive = receive_channel
    ctx.run_coros_to_execute = {}
    ctx.tasks = MagicMock()

    try:
        with pytest.raises(DisconnectedException):
            await asyncio.wait_for(
                ctx.create_poll_or_cancel_coroutine([0]),
                timeout=2.0,
            )

        # notify_input should never have been called with empty bytes
        for call in vm.notify_input.call_args_list:
            arg = call[0][0]
            assert len(arg) > 0, f"notify_input called with empty bytes: {arg!r}"
    finally:
        await receive_channel.close()
