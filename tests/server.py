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
import asyncio
import signal
from typing import Any, List, Optional
from types import FrameType
from unittest.mock import Mock

import pytest

from restate.endpoint import Endpoint

HEALTH_SCOPE: Any = {
    "type": "http",
    "asgi": {"version": "3.0"},
    "http_version": "1.1",
    "method": "GET",
    "scheme": "http",
    "path": "/restate/health",
    "raw_path": b"/restate/health",
    "query_string": b"",
    "headers": [],
    "client": ("127.0.0.1", 1234),
    "server": ("127.0.0.1", 9080),
}


async def drive_health_request(app: Any) -> List[Any]:
    """Send one health request through the app, returning the messages it sent.

    The SIGTERM handler is installed on the first request the app serves, so a
    request is how a test reaches that code path.
    """
    sent: List[Any] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(HEALTH_SCOPE, receive, send)
    return sent


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]


async def test_signal_handler_rejection_does_not_fail_request(monkeypatch: pytest.MonkeyPatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop,
        "add_signal_handler",
        Mock(side_effect=ValueError("add_signal_handler() can only be called from the main thread")),
    )

    app = Endpoint().app()
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/restate/health",
            "raw_path": b"/restate/health",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 9080),
        },
        receive,
        send,
    )

    response_starts = [message for message in sent if message["type"] == "http.response.start"]
    assert [message["status"] for message in response_starts] == [200]


@pytest.fixture
async def restore_sigterm():
    """SIGTERM disposition and the loop handler are process-global; put them back.

    Async so that teardown still runs inside the event loop, which
    ``remove_signal_handler`` needs.
    """
    original = signal.getsignal(signal.SIGTERM)
    try:
        yield
    finally:
        try:
            asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        signal.signal(signal.SIGTERM, original)


async def test_sigterm_redispatches_to_the_handler_it_displaced(restore_sigterm):
    """A host ASGI server's SIGTERM handler must survive our installing ours.

    ``loop.add_signal_handler`` installs a dummy handler through
    ``signal.signal``, so it silently replaces whatever the hosting server
    registered. uvicorn registers ``Server.handle_exit`` there, and losing it
    means the server never drains and gets force-killed by its supervisor.
    """
    host_handler_calls: List[int] = []

    def host_handler(signum: int, frame: Optional[FrameType]) -> None:
        host_handler_calls.append(signum)

    # Stand in for uvicorn's Server.capture_signals().
    signal.signal(signal.SIGTERM, host_handler)

    await drive_health_request(Endpoint().app())

    # Precondition: installing ours displaced theirs.
    assert signal.getsignal(signal.SIGTERM) is not host_handler

    signal.raise_signal(signal.SIGTERM)
    for _ in range(50):
        if host_handler_calls:
            break
        await asyncio.sleep(0.02)

    assert host_handler_calls == [signal.SIGTERM], "the displaced host handler was never re-dispatched"


async def test_sigterm_without_a_previous_handler_is_harmless(restore_sigterm):
    """Nothing to re-dispatch to is the common standalone case, not an error."""
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    await drive_health_request(Endpoint().app())

    signal.raise_signal(signal.SIGTERM)
    await asyncio.sleep(0.05)  # a raise that reached SIG_DFL would have killed us
