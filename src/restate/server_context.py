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
"""This module contains the restate context implementation based on the server"""

from typing import Any, Awaitable, Callable, List, TypeVar

import json

from restate.context import ObjectContext, Request
from restate.exceptions import TerminalError
from restate.handler import Handler
from restate.server_types import Receive, Send 
from restate.vm import Failure, Invocation, VMWrapper


T = TypeVar('T')
I = TypeVar('I')
O = TypeVar('O')

# disable to many arguments
# pylint: disable=R0913

# disable line too long
# pylint: disable=C0301


class ServerInvocationContext(ObjectContext):
    """This class implements the context for the restate framework based on the server."""

    def __init__(self,
                 vm: VMWrapper,
                 handler: Handler[I, O],
                 invocation: Invocation,
                 send: Send,
                 receive: Receive) -> None:
        super().__init__()
        self.vm = vm
        self.handler = handler
        self.invocation = invocation
        self.send = send
        self.receive = receive

    async def enter(self):
        """Invoke the user code."""
        try:
            in_buffer = self.invocation.input_buffer
            in_arg = self.handler.handler_io.deserializer(in_buffer)
            out_arg = await self.handler.fn(self, in_arg)
            out_buffer = self.handler.handler_io.serializer(out_arg)
            self.vm.sys_write_output(out_buffer)
        except TerminalError as t:
            failure = Failure(code=t.status_code, message=t.message)
            self.vm.sys_write_output(failure)
        # pylint: disable=W0718
        except Exception as e:
            self.vm.notify_error(str(e))
        self.vm.sys_end()
        while True:
            chunk = self.vm.take_output()
            if not chunk:
                break
            await self.send({
                'type': 'http.response.body',
                'body': chunk,
                'more_body': True,
            })
        # ========================================
        # End the connection
        # ========================================
        self.vm.dispose_callbacks()
        # The following sequence of events is expected during a teardown:
        #
        # {'type': 'http.request', 'body': b'', 'more_body': True}
        # {'type': 'http.request', 'body': b'', 'more_body': False}
        # {'type': 'http.disconnect'}
        while True:
            event = await self.receive()
            if not event:
                break
            if event['type'] == 'http.disconnect':
                break
            if event['type'] == 'http.request' and event['more_body'] is False:
                break
        # finally, we close our side
        # it is important to do it, after the other side has closed his side,
        # because some asgi servers (like hypercorn) will remove the stream 
        # as soon as they see a close event (in asgi terms more_body=False)
        await self.send({
            'type': 'http.response.body',
            'body': b'',
            'more_body': False,
        })

    async def create_poll_coroutine(self, handle) -> Awaitable[bytes | None]:
        """Create a coroutine to poll the handle."""
        output = self.vm.take_output()
        if output:
            await self.send({
                'type': 'http.response.body',
                'body': bytes(output),
                'more_body': True,
            })

        async def coro():
            """Wait for this handle to be resolved."""
            while True:
                chunk = await self.receive()
                assert isinstance(chunk['body'], bytes)
                self.vm.notify_input(chunk['body'])
                result = self.vm.take_async_result(handle)
                if result is None:
                    return None
                if isinstance(result, Failure):
                    raise TerminalError(result.message, result.code)
                return result

        return coro()

    async def get(self, name: str) -> Awaitable[T | None]:
        coro = await self.create_poll_coroutine(self.vm.sys_get(name))

        async def await_point():
            """Wait for this handle to be resolved."""
            res = await coro
            assert res is not None
            return json.loads(res)

        return await_point()
       
    async def state_keys(self) -> Awaitable[List[str]]:
        raise NotImplementedError

    async def set(self, name: str, value: T) -> None:
        raise NotImplementedError

    async def clear(self, name: str) -> None:
        raise NotImplementedError

    async def clear_all(self) -> None:
        raise NotImplementedError

    async def request(self) -> Request:
        raise NotImplementedError

    async def run_named(self, name: str, action: Callable[[], T] | Callable[[], Awaitable[T]]) -> Awaitable[T]:
        raise NotImplementedError

    async def sleep(self, millis: int) -> Awaitable[None]:
        raise NotImplementedError

    async def service_call(self, tpe: Callable[[Any, I], Awaitable[O]], arg: I) -> Awaitable[O]:
        raise NotImplementedError

    async def object_call(self, tpe: Callable[[Any, I], Awaitable[O]], key: str, arg: I) -> Awaitable[O]:
        raise NotImplementedError

    def key(self) -> str:
        raise NotImplementedError
