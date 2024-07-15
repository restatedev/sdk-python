#
#  Copyright (c) 2023-2024 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Node.js/TypeScript,
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


async def send404(send):
    """respond with a 404"""
    await send({
            'type': 'http.response.start',
            'status': 404,
            'headers': []})
    await send({
            'type': 'http.response.body',
            'body': b'',
            'more_body': False})

async def send_discovery(scope: Scope, send: Send, endpoint: Endpoint):
    """respond with a discovery"""
    discovered_as: Literal["request_response", "bidi"]
    if scope['http_version'] == '1.1':
        discovered_as = "request_response"
    else:
        discovered_as = "bidi"
    headers, js = compute_discovery_json(endpoint, 1, discovered_as)
    await send({
        'type': 'http.response.start',
        'status': 200,
        'headers': header_to_binary(headers.items()),
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
    await send({
        'type': 'http.response.start',
        'status': status,
        'headers': header_to_binary(res_headers),
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

    async def app(scope: Scope, receive: Receive, send: Send):
        try:
            if scope['type'] == 'lifespan':
                raise LifeSpanNotImplemented()
            if scope['type'] != 'http':
                raise NotImplementedError(f"Unknown scope type {scope['type']}")
            # might be a discovery request
            if scope['path'] == '/discover':
                await send_discovery(scope, send, endpoint)
                return
            # anything other than invoke is 404
            assert isinstance(scope['path'], str)
            if not scope['path'].startswith('/invoke/'):
                await send404(send)
                return
            # path is of the form: /invoke/:service/:handler
            # strip "/invoke/" (= strlen 8) and split the service and handler
            service_name, handler_name = scope['path'][8:].split('/')
            service = endpoint.services[service_name]
            if not service:
                await send404(send)
                return
            handler = service.handlers[handler_name]
            if not handler:
                await send404(send)
                return
            #
            # At this point we have a valid handler.
            # Let us setup restate's execution context for this invocation and handler.
            #
            assert not isinstance(scope['headers'], str)
            assert hasattr(scope['headers'], '__iter__')
            request_headers = binary_to_header(scope['headers'])
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

    return app
