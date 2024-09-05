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
from typing import Dict, Literal
import traceback
from restate.discovery import compute_discovery_json
from restate.endpoint import Endpoint
from restate.server_context import ServerInvocationContext
from restate.server_types import Receive, Scope, Send, binary_to_header, header_to_binary
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
    headers, js = compute_discovery_json(endpoint, 1, discovered_as)
    bin_headers = header_to_binary(headers.items())
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

async def process_invocation_to_completion(vm: VMWrapper,
                                           handler,
                                           attempt_headers: Dict[str, str],
                                           receive: Receive,
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
            assert isinstance(message['body'], bytes)
            vm.notify_input(message['body'])
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
        raise
    # pylint: disable=W0718
    except Exception:
        traceback.print_exc()
    await context.leave()

class LifeSpanNotImplemented(ValueError):
    """Signal to the asgi server that we didn't implement lifespans"""

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

            # Verify Identity
            assert not isinstance(scope['headers'], str)
            assert hasattr(scope['headers'], '__iter__')
            request_headers = binary_to_header(scope['headers'])
            try:
                identity_verifier.verify(request_headers, request_path)
            except IdentityVerificationException:
                # Identify verification failed, send back unauthorized and close
                await send_status(send, receive,401)
                return

            # might be a discovery request
            if request_path == '/discover':
                await send_discovery(scope, send, endpoint)
                return
            # anything other than invoke is 404
            if not request_path.startswith('/invoke/'):
                await send404(send, receive)
                return
            # path is of the form: /invoke/:service/:handler
            # strip "/invoke/" (= strlen 8) and split the service and handler
            service_name, handler_name = request_path[8:].split('/')
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
            await process_invocation_to_completion(VMWrapper(request_headers),
                                                   handler,
                                                   dict(request_headers),
                                                   receive,
                                                   send)
        except LifeSpanNotImplemented as e:
            raise e
        except Exception as e:
            traceback.print_exc()
            raise e

    if is_running_on_lambda():
        # If we're on Lambda, just return the adapter
        return wrap_asgi_as_lambda_handler(app)

    return app
