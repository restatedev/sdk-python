#
#  Copyright (c) 2023-2026 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#

"""Ingress client URL tests using an in-memory HTTP transport."""

from datetime import timedelta

import httpx
import pytest

from restate.client import Client


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("scope", [None, "tenant"])
@pytest.mark.parametrize("send", [False, True])
@pytest.mark.parametrize(
    "key, encoded",
    [
        ("customer/123", "customer%2F123"),
        ("customer?active=true", "customer%3Factive%3Dtrue"),
        ("customer#primary", "customer%23primary"),
        ("customer%2F123", "customer%252F123"),
        ("customer 123", "customer%20123"),
        ("customer-123", "customer-123"),
        ("café", "caf%C3%A9"),
    ],
)
async def test_client_preserves_object_key_in_url(key: str, encoded: str, send: bool, scope: str | None):
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return (
            httpx.Response(200, json={"invocationId": "invocation-123"}) if send else httpx.Response(200, content=b"ok")
        )

    async with httpx.AsyncClient(base_url="http://restate.invalid/", transport=httpx.MockTransport(handle)) as http:
        client = Client(http)
        if send:
            result = await client.generic_send(
                "Counter", "add", b"1", key=key, send_delay=timedelta(seconds=2), scope=scope
            )
            assert result.invocation_id == "invocation-123"
        else:
            assert await client.generic_call("Counter", "add", b"1", key=key, scope=scope) == b"ok"

    assert len(requests) == 1
    request = requests[0]
    if scope is None:
        expected = f"/Counter/{encoded}/add" + ("/send" if send else "")
    else:
        expected = f"/restate/scope/tenant/{'send' if send else 'call'}/Counter/{encoded}/add"
    if send:
        expected += "?delay=2000"
    assert request.url.raw_path == expected.encode("ascii")
    assert request.url.fragment == ""
    assert request.content == b"1"
