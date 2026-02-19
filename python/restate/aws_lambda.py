#
#  Copyright (c) 2023-2024 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""
This module contains the Lambda/ASGI adapter.
"""

import asyncio
import base64
import os
from typing import cast, Union, Any

from restate.server_types import (
    ASGIApp,
    Receive,
    RestateLambdaHandler,
    Scope,
    HTTPResponseStartEvent,
    HTTPResponseBodyEvent,
    HTTPRequestEvent,
    RestateLambdaRequest,
    RestateLambdaResponse,
)


def create_scope(req: RestateLambdaRequest) -> Scope:
    """
    Create ASGI scope from lambda request
    """
    headers = {k.lower(): v for k, v in req.get("headers", {}).items()}
    http_method = req["httpMethod"]
    path = req["path"]

    return {
        "type": "http",
        "method": http_method,
        "http_version": "1.1",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        "path": path,
        "scheme": headers.get("x-forwarded-proto", "https"),
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "client": None,
        "server": None,
        "extensions": None,
    }


def request_to_receive(req: RestateLambdaRequest) -> Receive:
    """
    Create ASGI Receive from lambda request
    """
    assert req["isBase64Encoded"]
    body = base64.b64decode(req["body"])

    # Decompress zstd-encoded request body
    headers = {k.lower(): v for k, v in req.get("headers", {}).items()}
    if "zstd" in headers.get("content-encoding", ""):
        body = zstd_decompress(body)

    events = cast(
        list[HTTPRequestEvent],
        [
            {"type": "http.request", "body": body, "more_body": False},
            {"type": "http.request", "body": b"", "more_body": False},
        ],
    )

    async def recv() -> HTTPRequestEvent:
        if len(events) != 0:
            return events.pop(0)
        # If we are out of events, return a future that will never complete
        f = asyncio.Future[HTTPRequestEvent]()
        return await f

    return recv


RESPONSE_COMPRESSION_THRESHOLD = 3 * 1024 * 1024


class ResponseCollector:
    """
    Response collector from ASGI Send to Lambda
    """

    def __init__(self, accept_encoding: str = ""):
        self.body = bytearray()
        self.headers: dict[str, str] = {}
        self.status_code = 500
        self.accept_encoding = accept_encoding

    async def __call__(self, message: Union[HTTPResponseStartEvent, HTTPResponseBodyEvent]) -> None:
        """
        Implements ASGI send contract
        """
        if message["type"] == "http.response.start":
            self.status_code = cast(int, message["status"])
            self.headers = {key.decode("utf-8"): value.decode("utf-8") for key, value in message["headers"]}
        elif message["type"] == "http.response.body" and "body" in message:
            self.body.extend(message["body"])
        return

    def to_lambda_response(self) -> RestateLambdaResponse:
        """
        Convert collected values to lambda response
        """
        body: bytes | bytearray = self.body

        # Compress response if it exceeds threshold and client accepts zstd
        if len(body) > RESPONSE_COMPRESSION_THRESHOLD and "zstd" in self.accept_encoding and zstd_available():
            body = zstd_compress(body)
            self.headers["content-encoding"] = "zstd"

        return {
            "statusCode": self.status_code,
            "headers": self.headers,
            "isBase64Encoded": True,
            "body": base64.b64encode(body).decode(),
        }


def is_running_on_lambda() -> bool:
    """
    :return: true if this Python script is running on Lambda
    """
    # https://docs.aws.amazon.com/lambda/latest/dg/configuration-envvars.html
    return "AWS_LAMBDA_FUNCTION_NAME" in os.environ


def wrap_asgi_as_lambda_handler(asgi_app: ASGIApp) -> RestateLambdaHandler:
    """
    Wrap the given asgi_app in a Lambda handler
    """
    # Setup AsyncIO
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def lambda_handler(event: RestateLambdaRequest, _context: Any) -> RestateLambdaResponse:
        loop = asyncio.get_event_loop()

        scope = create_scope(event)
        recv = request_to_receive(event)
        req_headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
        send = ResponseCollector(accept_encoding=req_headers.get("accept-encoding", ""))

        asgi_instance = asgi_app(scope, recv, send)
        asgi_task = loop.create_task(asgi_instance)  # type: ignore[var-annotated, arg-type]
        loop.run_until_complete(asgi_task)

        return send.to_lambda_response()

    return lambda_handler


def get_lambda_compression():
    """Return 'zstd' if running on Lambda and compression.zstd is available (Python 3.14+), else None."""
    if is_running_on_lambda() and zstd_available():
        return "zstd"
    return None


def zstd_available() -> bool:
    """Return True if zstd compression is available (Python 3.14+)."""
    try:
        import compression.zstd  # type: ignore[import-not-found]

        return compression.zstd is not None
    except ImportError:
        return False


def zstd_compress(data: bytes | bytearray) -> bytes:
    """Compress data using zstd."""
    try:
        import compression.zstd  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "zstd compression requested but compression.zstd is not available. "
            "Python 3.14+ is required for zstd compression support."
        ) from e
    return compression.zstd.compress(data)


def zstd_decompress(data: bytes) -> bytes:
    """Decompress zstd-compressed data."""
    try:
        import compression.zstd  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "Received zstd-compressed request but compression.zstd is not available. "
            "Python 3.14+ is required for zstd compression support."
        ) from e
    return compression.zstd.decompress(data)
