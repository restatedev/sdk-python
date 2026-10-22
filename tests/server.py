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
from unittest.mock import AsyncMock, Mock

import pytest
from restate import server
from restate.endpoint import Endpoint
from restate.server import LifeSpanNotImplemented


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


@pytest.mark.parametrize("registration", ["loop", "signal"])
async def test_sigterm_chains_host_after_notifying_channels(monkeypatch: pytest.MonkeyPatch, registration):
    loop = asyncio.get_running_loop()
    previous_signal = signal.getsignal(signal.SIGTERM)
    previous_handle = getattr(loop, "_signal_handlers", {}).get(signal.SIGTERM)
    calls = []
    host = Mock(side_effect=lambda *args: calls.append("host"))
    try:
        if registration == "loop":
            loop.add_signal_handler(signal.SIGTERM, host, "shutdown")
        else:
            loop.remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM, host)

        endpoint = Endpoint()
        endpoint.services["test"] = Mock()
        monkeypatch.setattr(server, "VMWrapper", Mock())

        async def process(vm, handler, headers, receive_channel, send, **kwargs):
            original_notify = receive_channel.notify_shutdown

            def notify():
                calls.append("channel")
                original_notify()

            monkeypatch.setattr(receive_channel, "notify_shutdown", notify)
            getattr(loop, "_signal_handlers", {})[signal.SIGTERM]._run()
            assert calls == ["channel", "host"]
            await asyncio.wait_for(receive_channel.block_until_http_input_closed(), 1)

        monkeypatch.setattr(server, "process_invocation_to_completion", process)
        receive = AsyncMock(return_value={"type": "http.disconnect"})
        await endpoint.app()(
            {"type": "http", "path": "/invoke/test/handler", "headers": []},
            receive,
            AsyncMock(),
        )
        if registration == "loop":
            host.assert_called_once_with("shutdown")
        else:
            host.assert_called_once_with(signal.SIGTERM, None)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        if previous_handle is not None:
            loop.add_signal_handler(signal.SIGTERM, previous_handle._callback, *previous_handle._args)
        else:
            signal.signal(signal.SIGTERM, previous_signal)


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
async def test_non_http_scope_does_not_install_signal_handler(monkeypatch: pytest.MonkeyPatch, scope_type):
    install = Mock()
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", install)
    expected = LifeSpanNotImplemented if scope_type == "lifespan" else NotImplementedError
    with pytest.raises(expected):
        await Endpoint().app()({"type": scope_type}, AsyncMock(), AsyncMock())
    install.assert_not_called()
