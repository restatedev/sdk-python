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
"""This module contains the ASGI server for the restate framework."""

import asyncio
from typing import Dict, TypedDict, Literal
import traceback
from restate.discovery import compute_discovery_json
from restate.endpoint import Endpoint
from restate.server_context import ServerInvocationContext, DisconnectedException
from restate.server_types import Receive, ReceiveChannel, Scope, Send, binary_to_header, header_to_binary # pylint: disable=line-too-long
from restate.vm import VMWrapper
from restate._internal import PyIdentityVerifier, IdentityVerificationException # pylint: disable=import-error,no-name-in-module
from restate._internal import SDK_VERSION # pylint: disable=import-error,no-name-in-module
from restate.aws_lambda import is_running_on_lambda, wrap_asgi_as_lambda_handler

X_RESTATE_SERVER = header_to_binary([("x-restate-server", f"restate-sdk-python/{SDK_VERSION}")])

async def send_status(send, receive, status_code: int):
    """respond with a status code"""
    await send({'type': 'http.response.start', 'status': status_code, "headers": X_RESTATE_SERVER})
    # For more info on why this loop, see ServerInvocationContext.leave()
    # pylint: disable=R0801
    while True:
        event = await receive()
        if event is None:
            break
        if event.get('type') == 'http.disconnect':
            break
        if event.get('type') == 'http.request' and event.get('more_body', False) is False:
            break
    await send({'type': 'http.response.body'})

async def send404(send, receive):
    """respond with a 404"""
    await send_status(send, receive, 404)

async def send_discovery(scope: Scope, send: Send, endpoint: Endpoint):
    """respond with a discovery"""
    discovered_as: Literal["request_response", "bidi"]
    if scope['http_version'] == '1.1':
        discovered_as = "request_response"
    else:
        discovered_as = "bidi"

    # Extract Accept header from request
    accept_header = None
    for header_name, header_value in binary_to_header(scope['headers']):
        if header_name.lower() == 'accept':
            accept_header = header_value
            break

    # Negotiate discovery protocol version
    version = 2
    if accept_header:
        if "application/vnd.restate.endpointmanifest.v3+json" in accept_header:
            version = 3
        elif "application/vnd.restate.endpointmanifest.v2+json" in accept_header:
            version = 2
        else:
            await send_status_with_error_text(
                send,
                415,
                f"Unsupported discovery version ${accept_header}")
            return

    try:
        js = compute_discovery_json(endpoint, version, discovered_as)
        bin_headers = header_to_binary([(
            "content-type",
            f"application/vnd.restate.endpointmanifest.v{version}+json")])
        bin_headers.extend(X_RESTATE_SERVER)
        await send({
            'type': 'http.response.start',
            'status': 200,
            'headers': bin_headers,
            'trailers': False
        })
        await send({
            'type': 'http.response.body',
            'body': js.encode('utf-8'),
            'more_body': False,
        })
    except ValueError as e:
        await send_status_with_error_text(send, 500, f"Error when computing discovery ${e}")
        return

async def send_status_with_error_text(send: Send, status_code: int, error_text: str):
    """respond with an health check"""
    headers = header_to_binary([("content-type", "text/plain")])
    headers.extend(X_RESTATE_SERVER)
    await send({
        'type': 'http.response.start',
        'status': status_code,
        'headers': headers,
        'trailers': False
    })
    await send({
        'type': 'http.response.body',
        'body': error_text.encode('utf-8'),
        'more_body': False,
    })

async def send_health_check(send: Send):
    """respond with an health check"""
    headers = header_to_binary([("content-type", "application/json")])
    headers.extend(X_RESTATE_SERVER)
    await send({
        'type': 'http.response.start',
        'status': 200,
        'headers': headers,
        'trailers': False
    })
    await send({
        'type': 'http.response.body',
        'body': b'{"status":"ok"}',
        'more_body': False,
    })


async def process_invocation_to_completion(vm: VMWrapper,
                                           handler,
                                           attempt_headers: Dict[str, str],
                                           receive: ReceiveChannel,
                                           send: Send):
    """Invoke the user code."""
    status, res_headers = vm.get_response_head()
    res_bin_headers = header_to_binary(res_headers)
    res_bin_headers.extend(X_RESTATE_SERVER)
    await send({
        'type': 'http.response.start',
        'status': status,
        'headers': res_bin_headers,
        'trailers': False
    })
    assert status == 200
    # ========================================
    # Read the input and the journal
    # ========================================
    while True:
        message = await receive()
        if message.get('type') == 'http.disconnect':
            # everything ends here really ...
            return
        if message.get('type') == 'http.request':
            body = message.get('body', None)
            assert isinstance(body, bytes)
            vm.notify_input(body)
        if not message.get('more_body', False):
            vm.notify_input_closed()
            break
        if vm.is_ready_to_execute():
            break
    # ========================================
    # Execute the user code
    # ========================================
    invocation = vm.sys_input()
    context = ServerInvocationContext(vm=vm,
                                      handler=handler,
                                      invocation=invocation,
                                      attempt_headers=attempt_headers,
                                      send=send,
                                      receive=receive)
    try:
        await context.enter()
    except asyncio.exceptions.CancelledError:
        context.on_attempt_finished()
        raise
    except DisconnectedException:
        # The client disconnected before we could send the response
        context.on_attempt_finished()
        return
    # pylint: disable=W0718
    except Exception:
        traceback.print_exc()
    try:
        await context.leave()
    finally:
        context.on_attempt_finished()

class LifeSpanNotImplemented(ValueError):
    """Signal to the asgi server that we didn't implement lifespans"""


class ParsedPath(TypedDict):
    """Parsed path from the request."""
    type: Literal["invocation", "health", "discover", "unknown"]
    service: str | None
    handler: str | None

def parse_path(request: str) -> ParsedPath:
    """Parse the path from the request."""
    # The following routes are possible
    # $mountpoint/health
    # $mountpoint/discover
    # $mountpoint/invoke/:service/:handler
    # as we don't know the mountpoint, we need to check the path carefully
    fragments = request.rsplit('/', 4)
    # /invoke/:service/:handler
    if len(fragments) >= 3 and fragments[-3] == 'invoke':
        return { "type": "invocation" , "handler" : fragments[-1], "service" : fragments[-2] }
    # /health
    if fragments[-1] == 'health':
        return { "type": "health", "service": None, "handler": None }
    # /discover
    if fragments[-1] == 'discover':
        return { "type": "discover" , "service": None, "handler": None }
    # anything other than invoke is 404
    return { "type": "unknown" , "service": None, "handler": None }


def asgi_app(endpoint: Endpoint):
    """Create an ASGI-3 app for the given endpoint."""

    # Prepare request signer
    identity_verifier = PyIdentityVerifier(endpoint.identity_keys)

    async def app(scope: Scope, receive: Receive, send: Send):
        try:
            if scope['type'] == 'lifespan':
                raise LifeSpanNotImplemented()
            if scope['type'] != 'http':
                raise NotImplementedError(f"Unknown scope type {scope['type']}")

            request_path = scope['path']
            assert isinstance(request_path, str)
            request: ParsedPath = parse_path(request_path)

            # Health check
            if request['type'] == 'health':
                await send_health_check(send)
                return

            # Verify Identity
            assert not isinstance(scope['headers'], str)
            assert hasattr(scope['headers'], '__iter__')
            request_headers = binary_to_header(scope['headers'])
            try:
                identity_verifier.verify(request_headers, request_path)
            except IdentityVerificationException:
                # Identify verification failed, send back unauthorized and close
                await send_status(send, receive, 401)
                return

            # might be a discovery request
            if request['type'] == 'discover':
                await send_discovery(scope, send, endpoint)
                return
            # anything other than invoke is 404
            if request['type'] == 'unknown':
                await send404(send, receive)
                return
            assert request['type'] == 'invocation'
            assert request['service'] is not None
            assert request['handler'] is not None
            service_name, handler_name = request['service'], request['handler']
            service = endpoint.services.get(service_name)
            if not service:
                await send404(send, receive)
                return
            handler = service.handlers.get(handler_name)
            if not handler:
                await send404(send, receive)
                return
            #
            # At this point we have a valid handler.
            # Let us setup restate's execution context for this invocation and handler.
            #
            receive_channel = ReceiveChannel(receive)
            try:
                await process_invocation_to_completion(VMWrapper(request_headers),
                                                           handler,
                                                           dict(request_headers),
                                                           receive_channel,
                                                           send)
            finally:
                await receive_channel.close()
        except LifeSpanNotImplemented as e:
            raise e
        except Exception as e:
            traceback.print_exc()
            raise e

    if is_running_on_lambda():
        # If we're on Lambda, just return the adapter
        return wrap_asgi_as_lambda_handler(app)

    return app
