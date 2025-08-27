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
from typing import TypedDict, Dict, cast, Union, Any, Callable

from restate.server_types import (ASGIApp,
                                  Scope,
                                  Receive,
                                  HTTPResponseStartEvent,
                                  HTTPResponseBodyEvent,
                                  HTTPRequestEvent)

class RestateLambdaRequest(TypedDict):
    """
    Restate Lambda request

    :see: https://github.com/restatedev/restate/blob/1a10c05b16b387191060b49faffb0335ee97e96d/crates/service-client/src/lambda.rs#L297 # pylint: disable=line-too-long
    """
    path: str
    httpMethod: str
    headers: Dict[str, str]
    body: str
    isBase64Encoded: bool


class RestateLambdaResponse(TypedDict):
    """
    Restate Lambda response

    :see: https://github.com/restatedev/restate/blob/1a10c05b16b387191060b49faffb0335ee97e96d/crates/service-client/src/lambda.rs#L310 # pylint: disable=line-too-long
    """
    statusCode: int
    headers: Dict[str, str]
    body: str
    isBase64Encoded: bool


RestateLambdaHandler = Callable[[RestateLambdaRequest, Any], RestateLambdaResponse]


def create_scope(req: RestateLambdaRequest) -> Scope:
    """
    Create ASGI scope from lambda request
    """
    headers = {k.lower(): v for k, v in req.get('headers', {}).items()}
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
        "query_string": b'',
        "client": None,
        "server": None,
        "extensions": None
    }


def request_to_receive(req: RestateLambdaRequest) -> Receive:
    """
    Create ASGI Receive from lambda request
    """
    assert req['isBase64Encoded']
    body = base64.b64decode(req['body'])

    events = cast(list[HTTPRequestEvent], [{
        "type": "http.request",
        "body": body,
        "more_body": False
    },
    {
        "type": "http.request",
        "body": b'',
        "more_body": False
    }])

    async def recv() -> HTTPRequestEvent:
        if len(events) != 0:
            return events.pop(0)
        # If we are out of events, return a future that will never complete
        f = asyncio.Future[HTTPRequestEvent]()
        return await f

    return recv


class ResponseCollector:
    """
    Response collector from ASGI Send to Lambda
    """
    def __init__(self):
        self.body = bytearray()
        self.headers = {}
        self.status_code = 500

    async def __call__(self, message: Union[HTTPResponseStartEvent, HTTPResponseBodyEvent]) -> None:
        """
        Implements ASGI send contract
        """
        if message["type"] == "http.response.start":
            self.status_code = cast(int, message["status"])
            self.headers = {
                key.decode("utf-8"): value.decode("utf-8")
                for key, value in message["headers"]
            }
        elif message["type"] == "http.response.body" and "body" in message:
            self.body.extend(message["body"])
        return

    def to_lambda_response(self) -> RestateLambdaResponse:
        """
        Convert collected values to lambda response
        """
        return {
            "statusCode": self.status_code,
            "headers": self.headers,
            "isBase64Encoded": True,
            "body": base64.b64encode(self.body).decode()
        }


def is_running_on_lambda() -> bool:
    """
    :return: true if this Python script is running on Lambda
    """
    # https://docs.aws.amazon.com/lambda/latest/dg/configuration-envvars.html
    return "AWS_LAMBDA_FUNCTION_NAME" in os.environ


def wrap_asgi_as_lambda_handler(asgi_app: ASGIApp) \
        -> Callable[[RestateLambdaRequest, Any], RestateLambdaResponse]:
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
        send = ResponseCollector()

        asgi_instance = asgi_app(scope, recv, send)
        asgi_task = loop.create_task(asgi_instance)  # type: ignore[var-annotated, arg-type]
        loop.run_until_complete(asgi_task)

        return send.to_lambda_response()

    return lambda_handler
