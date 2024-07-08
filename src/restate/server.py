"""This module contains the ASGI server for the restate framework."""

import typing
from restate.discovery import compute_discovery_json
from restate.endpoint import Endpoint
from restate.vm import VMWrapper

#pylint: disable=C0301

def header_to_binary(headers: typing.Iterable[typing.Tuple[str, str]]) -> typing.List[typing.Tuple[bytes, bytes]]:
    """Convert a list of headers to a list of binary headers."""
    return [ (k.encode('utf-8'), v.encode('utf-8')) for k,v in headers ]

def binary_to_header(headers: typing.Iterable[typing.Tuple[bytes, bytes]]) -> typing.List[typing.Tuple[str, str]]:
    """Convert a list of binary headers to a list of headers."""
    return [ (k.decode('utf-8'), v.decode('utf-8')) for k,v in headers ]

def asgi_app(endpoint: Endpoint):
    """Create an ASGI-3 app for the given endpoint."""

    async def send404(send):
        await send({
            'type': 'http.response.start',
            'status': 404,
            'headers': [],
        })
        await send({
            'type': 'http.response.body',
            'body': b'',
            'more_body': False,
        })

    async def app(scope, receive, send):
        if scope['type'] != 'http':
            raise NotImplementedError(f"Unknown scope type {scope['type']}")

        # might be a discovery request
        if scope['path'] == '/discover':
            discovered_as: typing.Literal['bidi', 'request_response'] = "request_response" if scope['http_version'] == '1.1' else "bidi"
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
            return
        # anything other than invoke is 404
        if not scope['path'].startswith('/invoke/'):
            await send404(send)
            return
        service_name, handler_name = scope['path'][len('/invoke/'):].split('/')
        service = endpoint.services[service_name]
        if not service:
            await send404(send)
            return
        handler = service.handlers[handler_name]
        if not handler:
            send404(send)
            return
        #
        # At this point we have a valid handler.
        # Let us setup restate's execution context for this invocation and handler.
        #
        vm = VMWrapper(binary_to_header(scope['headers']))
        status, res_headers = vm.get_response_head()
        await send({
            'type': 'http.response.start',
            'status': status,
            'headers': header_to_binary(res_headers),
        })
        assert status == 200
        #
        # read while:
        # - VM is not ready
        # - or there is no more body
        # - the client has disconnected
        #
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                # everything ends here really ...
                vm.dispose_callbacks()
                return
            if message['type'] == 'http.request':
                vm.notify_input(message['body'])
            if not message.get('more_body', False):
                vm.notify_input_closed()
                break
            if vm.take_is_ready_to_execute():
                break
        # lets run the user code
        invocation = vm.sys_input()
        # lets call the user code
        in_arg = handler.handler_io.deserializer(invocation.input_buffer)


    

        out_arg = await handler.fn(None, in_arg)


        # lets serialize the output
        out_invocation = handler.handler_io.serializer(out_arg)
        vm.sys_write_output(out_invocation)
        vm.sys_end()
        while True:
            chunk = vm.take_output()
            if not chunk:
                break
            await send({
                'type': 'http.response.body',
                'body': chunk,
                'more_body': True,
            })
        # end the connection
        vm.dispose_callbacks()

        # The following sequence of events is expected during a teardown:
        #
        # {'type': 'http.request', 'body': b'', 'more_body': True}
        # {'type': 'http.request', 'body': b'', 'more_body': False}
        # {'type': 'http.disconnect'}
        while True:
            event = await receive()
            if not event:
                break
            if event['type'] == 'http.disconnect':
                break
            if event['type'] == 'http.request' and event['more_body'] is False:
                break
        #
        # finally, we close our side
        # it is important to do it, after the other side has closed his side,
        # because some asgi servers (like hypercorn) will remove the stream 
        # as soon as they see a close event (in asgi terms more_body=False)
        await send({
            'type': 'http.response.body',
            'body': b'',
            'more_body': False,
        })
        return
    return app
