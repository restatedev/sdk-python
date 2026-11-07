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
"""Per-call headers are reusable across handlers with different content types."""

import httpx2
import pytest

import restate
from restate.client import Client
from restate.serde import BytesSerde


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


echo = restate.Service("echo")


@echo.handler()
async def text(ctx: restate.Context, value: str) -> str:
    return value


@echo.handler(
    accept="application/octet-stream",
    content_type="application/octet-stream",
    input_serde=BytesSerde(),
    output_serde=BytesSerde(),
)
async def binary(ctx: restate.Context, value: bytes) -> bytes:
    return value


@pytest.mark.anyio
@pytest.mark.parametrize("header_name", [None, "Content-Type", "content-type", "CONTENT-TYPE"])
async def test_call_headers_are_preserved(header_name: str | None):
    headers = {"X-Correlation-Id": "shared"}
    if header_name is not None:
        headers[header_name] = "application/custom"
    original = headers.copy()
    content_types: list[list[str]] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        content_types.append(request.headers.get_list("content-type"))
        return httpx2.Response(200, content=request.content)

    async with httpx2.AsyncClient(base_url="http://restate.test", transport=httpx2.MockTransport(respond)) as http:
        client = Client(http)
        assert await client.service_call(text, "hello", headers=headers) == "hello"
        assert await client.service_call(binary, b"hello", headers=headers) == b"hello"

    expected = (
        [["application/json"], ["application/octet-stream"]]
        if header_name is None
        else [["application/custom"], ["application/custom"]]
    )
    assert (headers, content_types) == (original, expected)
