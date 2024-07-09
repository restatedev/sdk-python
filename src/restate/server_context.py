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

import inspect
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar
import json
import typing

from restate.context import ObjectContext, Request, Serde
from restate.exceptions import TerminalError
from restate.handler import Handler, invoke_handler
from restate.serde import JsonSerde
from restate.server_types import Receive, Send
from restate.vm import Failure, Invocation, NotReady, SuspendedException, VMWrapper


T = TypeVar('T')
I = TypeVar('I')
O = TypeVar('O')

# disable to many arguments
# pylint: disable=R0913

# disable line too long
# pylint: disable=C0301

async def async_value(n: Callable[[], T]) -> T:
    """convert a simple value to a coroutine."""
    return n()

class ServerInvocationContext(ObjectContext):
    """This class implements the context for the restate framework based on the server."""

    def __init__(self,
                 vm: VMWrapper,
                 handler: Handler[I, O],
                 invocation: Invocation,
                 attempt_headers: Dict[str, str],
                 send: Send,
                 receive: Receive) -> None:
        super().__init__()
        self.vm = vm
        self.handler = handler
        self.invocation = invocation
        self.attempt_headers = attempt_headers
        self.send = send
        self.receive = receive

    async def enter(self):
        """Invoke the user code."""
        try:
            in_buffer = self.invocation.input_buffer
            out_buffer = await invoke_handler(handler=self.handler, ctx=self, in_buffer=in_buffer)
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
        coro = self.create_poll_coroutine(self.vm.sys_get_state(name))

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
        self.vm.sys_set_state(name, bytes(buffer))

    def clear(self, name: str) -> None:
        raise NotImplementedError

    def clear_all(self) -> None:
        raise NotImplementedError

    def request(self) -> Request:
        return Request(
            id=self.invocation.invocation_id,
            headers=dict(self.invocation.headers),
            attempt_headers=self.attempt_headers,
            body=self.invocation.input_buffer,
        )

    # pylint: disable=W0236
    async def run(self,
                  name: str,
                  action: Callable[[], T] | Callable[[], Awaitable[T]],
                  serde: Optional[Serde[T]] = JsonSerde()) -> T |  None:
        assert serde is not None
        res = self.vm.sys_run_enter(name)
        if isinstance(res, Failure):
            raise TerminalError(res.message, res.code)
        if isinstance(res, bytes):
            return serde.deserialize(res)
        # the side effect was not executed before, so we need to execute it now
        assert res is None
        try:
            if inspect.iscoroutinefunction(action):
                action_result = await action() # type: ignore
            else:
                action_result = action()
            buffer = serde.serialize(action_result)
            handle = self.vm.sys_run_exit_success(buffer)
            await self.create_poll_coroutine(handle)
            return action_result
        except TerminalError as t:
            failure = Failure(code=t.status_code, message=t.message)
            handle = self.vm.sys_run_exit_failure(failure)
            await self.create_poll_coroutine(handle)
            # unreachable
            assert False

    def sleep(self, millis: int) -> Awaitable[None]:
        raise NotImplementedError

    def service_call(self, tpe: Callable[[Any, I], Awaitable[O]], arg: I) -> Awaitable[O]:
        raise NotImplementedError

    def object_call(self, tpe: Callable[[Any, I], Awaitable[O]], key: str, arg: I) -> Awaitable[O]:
        raise NotImplementedError

    def key(self) -> str:
        return self.invocation.key
