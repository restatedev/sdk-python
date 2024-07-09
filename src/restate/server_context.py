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
import typing

from restate.context import ObjectContext, Request
from restate.exceptions import TerminalError
from restate.handler import Handler
from restate.server_types import Receive, Send 
from restate.vm import Failure, Invocation, NotReady, SuspendedException, VMWrapper


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
            in_arg = self.handler.handler_io.deserializer(in_buffer) # type: ignore
            out_arg = await self.handler.fn(self, in_arg) # type: ignore
            out_buffer = self.handler.handler_io.serializer(out_arg) # type: ignore
            self.vm.sys_write_output_success(bytes(out_buffer))
            self.vm.sys_end()
        except TerminalError as t:
            failure = Failure(code=t.status_code, message=t.message)
            self.vm.sys_write_output_failure(failure)
            self.vm.sys_end()
        # pylint: disable=W0718
        except SuspendedException:
            pass
        except Exception as e:
            self.vm.notify_error(str(e))
            # no need to call sys_end here, because the error will be propagated

    async def leave(self):
        """Leave the context."""
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


    async def create_poll_coroutine(self, handle) -> bytes | None:
        """Create a coroutine to poll the handle."""
        output = self.vm.take_output()
        if output:
            await self.send({
                'type': 'http.response.body',
                'body': bytes(output),
                'more_body': True,
            })
        self.vm.notify_await_point(handle)
        while True:
            res = self.vm.take_async_result(handle)
            if isinstance(res, NotReady):
                chunk = await self.receive()
                if chunk['body']:
                    assert isinstance(chunk['body'], bytes)
                    self.vm.notify_input(chunk['body'])
                if not chunk.get('more_body', False):
                    self.vm.notify_input_closed()
                continue
            if res is None:
                return None
            if isinstance(res, Failure):
                raise TerminalError(res.message, res.code)
            return res

    def get(self, name: str) -> typing.Awaitable[typing.Any | None]:
        coro = self.create_poll_coroutine(self.vm.sys_get(name))

        async def await_point():
            """Wait for this handle to be resolved."""
            res = await coro
            if res:
                return json.loads(res.decode('utf-8'))
            return None

        return await_point() # do not await here, the caller will do it.

    def state_keys(self) -> Awaitable[List[str]]:
        raise NotImplementedError

    def set(self, name: str, value: T) -> None:
        """Set the value associated with the given name."""
        buffer = json.dumps(value).encode('utf-8')
        self.vm.sys_set(name, bytes(buffer))

    def clear(self, name: str) -> None:
        raise NotImplementedError

    def clear_all(self) -> None:
        raise NotImplementedError

    def request(self) -> Request:
        raise NotImplementedError

    def run_named(self, name: str, action: Callable[[], T] | Callable[[], Awaitable[T]]) -> Awaitable[T]:
        raise NotImplementedError

    def sleep(self, millis: int) -> Awaitable[None]:
        raise NotImplementedError

    def service_call(self, tpe: Callable[[Any, I], Awaitable[O]], arg: I) -> Awaitable[O]:
        raise NotImplementedError

    def object_call(self, tpe: Callable[[Any, I], Awaitable[O]], key: str, arg: I) -> Awaitable[O]:
        raise NotImplementedError

    def key(self) -> str:
        raise NotImplementedError
