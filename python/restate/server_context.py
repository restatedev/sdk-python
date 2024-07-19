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

from datetime import timedelta
import inspect
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar
import typing
import traceback

from restate.context import DurablePromise, ObjectContext, Request
from restate.exceptions import TerminalError
from restate.handler import Handler, handler_from_callable, invoke_handler
from restate.serde import BytesSerde, JsonSerde, Serde
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


class ServerDurablePromise(DurablePromise):
    """This class implements a durable promise API"""

    def __init__(self, server_context, name, serde) -> None:
        super().__init__(name=name, serde=JsonSerde() if serde is None else serde)
        self.server_context = server_context

    def value(self) -> Awaitable[Any]:
        vm: VMWrapper = self.server_context.vm
        handle = vm.sys_get_promise(self.name)
        coro =  self.server_context.create_poll_coroutine(handle)
        serde = self.serde
        assert serde is not None

        async def await_point():
            res = await coro
            return serde.deserialize(res)

        return await_point()

    def resolve(self, value: Any) -> Awaitable[None]:
        vm: VMWrapper = self.server_context.vm
        assert self.serde is not None
        value_buffer = self.serde.serialize(value)
        handle = vm.sys_complete_promise_success(self.name, value_buffer)
        return self.server_context.create_poll_coroutine(handle)

    def reject(self, message: str, code: int = 500) -> Awaitable[None]:
        vm: VMWrapper = self.server_context.vm
        py_failure = Failure(code=code, message=message)
        handle = vm.sys_complete_promise_failure(self.name, py_failure)
        return self.server_context.create_poll_coroutine(handle)

    def peek(self) -> Awaitable[Any | None]:
        vm: VMWrapper = self.server_context.vm
        handle = vm.sys_peek_promise(self.name)
        coro =  self.server_context.create_poll_coroutine(handle)
        serde = self.serde
        assert serde is not None

        async def await_point():
            res = await coro
            if res is None:
                return None
            return serde.deserialize(res)

        return await_point()


# disable too many public method
# pylint: disable=R0904

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
            fmt = '\n'.join(traceback.format_exception(e))
            self.vm.notify_error(fmt)
            raise e

    async def leave(self):
        """Leave the context."""
        while True:
            chunk = self.vm.take_output()
            if chunk is None:
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
            if event is None:
                break
            if event.get('type') == 'http.disconnect':
                break
            if event.get('type') == 'http.request' and event.get('more_body', False) is False:
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
                if chunk.get('body', None) is not None:
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

    def get(self, name: str, serde: Serde[T] = JsonSerde()) -> typing.Awaitable[Optional[Any]]:
        coro = self.create_poll_coroutine(self.vm.sys_get_state(name))

        async def await_point():
            """Wait for this handle to be resolved."""
            res = await coro
            if res is None:
                return None
            return serde.deserialize(res)

        return await_point() # do not await here, the caller will do it.

    def state_keys(self) -> Awaitable[List[str]]:
        raise NotImplementedError

    def set(self, name: str, value: T, serde: Serde[T] = JsonSerde()) -> None:
        """Set the value associated with the given name."""
        buffer = serde.serialize(value)
        self.vm.sys_set_state(name, bytes(buffer))

    def clear(self, name: str) -> None:
        self.vm.sys_clear_state(name)

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
                  serde: Optional[Serde[T]] = JsonSerde()) -> T | None:
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

    def sleep(self, delta: timedelta) -> Awaitable[None]:
        # convert timedelta to milliseconds
        millis = int(delta.total_seconds() * 1000)
        return self.create_poll_coroutine(self.vm.sys_sleep(millis)) # type: ignore

    def do_call(self,
                tpe: Callable[[Any, I], Awaitable[O]],
                parameter: I,
                key: Optional[str] = None,
                send_delay: Optional[timedelta] = None,
                send: bool = False) -> Awaitable[O] | None:
        """Make an RPC call to the given handler"""
        target_handler = handler_from_callable(tpe)
        service=target_handler.service_tag.name
        handler=target_handler.name
        input_serde = target_handler.handler_io.input_serde
        output_serde = target_handler.handler_io.output_serde
        return self.do_raw_call(service, handler, parameter, input_serde, output_serde, key, send_delay, send)


    def do_raw_call(self,
                 service: str,
                 handler:str,
                 input_param: I,
                 input_serde: Serde[I],
                 output_serde: Serde[O],
                 key: Optional[str] = None,
                 send_delay: Optional[timedelta] = None,
                 send: bool = False) -> Awaitable[O] | None:
        """Make an RPC call to the given handler"""
        parameter = input_serde.serialize(input_param)
        if send_delay:
            ms = int(send_delay.total_seconds() * 1000)
            self.vm.sys_send(service, handler, parameter, key, delay=ms)
            return None
        if send:
            self.vm.sys_send(service, handler, parameter, key)
            return None

        handle = self.vm.sys_call(service=service,
                                  handler=handler,
                                  parameter=parameter,
                                  key=key)

        async def await_point(s: ServerInvocationContext, h, o: Serde[O]):
            """Wait for this handle to be resolved, and deserialize the response."""
            res = await s.create_poll_coroutine(h)
            return o.deserialize(res) # type: ignore

        return await_point(self, handle, output_serde)

    def service_call(self,
                     tpe: Callable[[Any, I], Awaitable[O]],
                     arg: I) -> Awaitable[O]:
        coro = self.do_call(tpe, arg)
        assert coro is not None
        return coro

    def service_send(self, tpe: Callable[[Any, I], Awaitable[O]], arg: I, send_delay: timedelta | None = None) -> None:
        self.do_call(tpe=tpe, parameter=arg, send_delay=send_delay, send=True)

    def object_call(self,
                    tpe: Callable[[Any, I],Awaitable[O]],
                    key: str,
                    arg: I,
                    send_delay: Optional[timedelta] = None,
                    send: bool = False) -> Awaitable[O]:
        coro = self.do_call(tpe, arg, key, send_delay, send)
        assert coro is not None
        return coro

    def object_send(self, tpe: Callable[[Any, I], Awaitable[O]], key: str, arg: I, send_delay: timedelta | None = None) -> None:
        self.do_call(tpe=tpe, key=key, parameter=arg, send_delay=send_delay, send=True)

    def workflow_call(self,
                        tpe: Callable[[Any, I], Awaitable[O]],
                        key: str,
                        arg: I) -> Awaitable[O]:
        return self.object_call(tpe, key, arg)

    def workflow_send(self, tpe: Callable[[Any, I], Awaitable[O]], key: str, arg: I, send_delay: timedelta | None = None) -> None:
        return self.object_send(tpe, key, arg, send_delay)

    def generic_call(self, service: str, handler: str, arg: bytes, key: str | None = None) -> Awaitable[bytes]:
        serde = BytesSerde()
        return self.do_raw_call(service, handler, arg, serde, serde, key) # type: ignore

    def generic_send(self, service: str, handler: str, arg: bytes, key: str | None = None, send_delay: timedelta | None = None) -> None:
        serde = BytesSerde()
        return self.do_raw_call(service, handler, arg, serde, serde , key, send_delay, True) # type: ignore

    def awakeable(self,
                  serde: typing.Optional[Serde[I]] = JsonSerde()) -> typing.Tuple[str, Awaitable[Any]]:
        assert serde is not None
        name, handle = self.vm.sys_awakeable()
        coro = self.create_poll_coroutine(handle)

        async def await_point():
            """Wait for this handle to be resolved."""
            res = await coro
            assert res is not None
            return serde.deserialize(res)


        return name, await_point()

    def resolve_awakeable(self,
                          name: str,
                          value: I,
                          serde: typing.Optional[Serde[I]] = JsonSerde()) -> None:
        assert serde is not None
        buf = serde.serialize(value)
        self.vm.sys_resolve_awakeable(name, buf)

    def reject_awakeable(self, name: str, failure_message: str, failure_code: int = 500) -> None:
        return self.vm.sys_reject_awakeable(name, Failure(code=failure_code, message=failure_message))

    def promise(self, name: str, serde: typing.Optional[Serde[T]] = JsonSerde()) -> DurablePromise[Any]:
        """Create a durable promise."""
        return ServerDurablePromise(self, name, serde)

    def key(self) -> str:
        return self.invocation.key
