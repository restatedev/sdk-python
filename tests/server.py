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
from unittest.mock import Mock

import pytest

from restate.endpoint import Endpoint


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

    assert {message.get("status") for message in sent} == {200}
