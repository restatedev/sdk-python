
import typing
from typing import AsyncGenerator
import asyncio

from starlette.applications import Starlette
from starlette.responses import StreamingResponse, Response
from starlette.requests import Request
from starlette.routing import Route


from restate.discovery import compute_discovery_json
from restate.endpoint import Endpoint
from restate.handler import Handler
from restate.vm import VMWrapper

def server(endpoint: Endpoint):

    async def discover(request: Request):
        accept = request.headers.get("accept")
        if not accept:
            return Response(status_code=400, content="Content-Type header is required")
        if accept != "application/vnd.restate.endpointmanifest.v1+json":
            return Response(status_code=400, content="Unsupported version")
        headers, js = compute_discovery_json(endpoint, 1)
        return Response(content=js, status_code=200, headers=headers)


    async def run_user_code(vm: VMWrapper, handler: Handler):
        invocation = vm.sys_input()
        # lets call the user code
        in_arg = handler.handler_io.deserializer(invocation.input_buffer)
        out_arg = await handler.fn(None, in_arg)
        # lets serialize the output
        out_invocation = handler.handler_io.serializer(out_arg)
        vm.sys_write_output(out_invocation)
        vm.sys_end()
        chunk = vm.take_output()
        assert vm.take_output() is None
        return chunk

    async def invoke(request: Request) -> Response:
        service_name = request.path_params['service']
        service = endpoint.services[service_name]
        if not service:
            return Response(status_code=404)
        handler_name = request.path_params['handler']
        handler = service.handlers[handler_name]
        if not handler:
            return Response(status_code=404)

        # LFG
        vm = VMWrapper(request.headers.items())
        status, headers = vm.get_response_head()
        # read the entire request body as per H1
        stream = request.stream()
        async for chunk in stream:
            vm.notify_input(chunk)
            if vm.take_is_ready_to_execute():
                break
        vm.notify_input_closed()

        out_bytes = await run_user_code(vm, handler)
        return Response(headers=dict(headers),
                                 status_code=status,
                                 content=out_bytes)

    return Starlette(debug=True, routes=[
        Route('/discover', discover, methods=["GET"]),
        Route('/invoke/{service}/{handler}', invoke, methods=["POST"]),
    ])
