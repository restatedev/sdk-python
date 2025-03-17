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
# pylint: disable=R0917
"""This module contains the restate context implementation based on the server"""

import asyncio
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
from restate.vm import Failure, Invocation, NotReady, SuspendedException, VMWrapper, RunRetryConfig, CANCEL_HANDLE # pylint: disable=line-too-long
from restate._internal import PyDoProgressAnyCompleted, PyDoProgressReadFromInput, PyDoProgressExecuteRun, PyDoProgressCancelSignalReceived # pylint: disable=import-error,no-name-in-module,line-too-long

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
        coro =  self.server_context.create_poll_or_cancel_coroutine(handle)
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
        return self.server_context.create_poll_or_cancel_coroutine(handle)

    def reject(self, message: str, code: int = 500) -> Awaitable[None]:
        vm: VMWrapper = self.server_context.vm
        py_failure = Failure(code=code, message=message)
        handle = vm.sys_complete_promise_failure(self.name, py_failure)
        return self.server_context.create_poll_or_cancel_coroutine(handle)

    def peek(self) -> Awaitable[Any | None]:
        vm: VMWrapper = self.server_context.vm
        handle = vm.sys_peek_promise(self.name)
        coro =  self.server_context.create_poll_or_cancel_coroutine(handle)
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
        self.run_coros_to_execute: dict[int, Awaitable[typing.Union[bytes | Failure]]] = {}

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
            stacktrace = '\n'.join(traceback.format_exception(e))
            self.vm.notify_error(repr(e), stacktrace)
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

    async def take_and_send_output(self):
        """Take output from state machine and send it"""
        output = self.vm.take_output()
        if output:
            await self.send({
                'type': 'http.response.body',
                'body': bytes(output),
                'more_body': True,
            })

    def must_take_notification(self, handle):
        """Take notification, which must be present"""
        res = self.vm.take_notification(handle)
        if isinstance(res, NotReady):
            raise ValueError(f"Unexpected value error: {handle}")
        if res is None:
            return None
        if isinstance(res, Failure):
            raise TerminalError(res.message, res.code)
        return res

    async def create_poll_or_cancel_coroutine(self, handle) -> bytes | None:
        """Create a coroutine to poll the handle."""
        await self.take_and_send_output()
        while True:
            if self.vm.is_completed(handle):
                # Handle is completed
                return self.must_take_notification(handle)

            # Nothing ready yet, let's try to make some progress
            do_progress_response = self.vm.do_progress([handle, CANCEL_HANDLE])
            if isinstance(do_progress_response, PyDoProgressAnyCompleted):
                # One of the handles completed, we can continue
                continue
            if isinstance(do_progress_response, PyDoProgressCancelSignalReceived):
                # Raise cancel signal
                raise TerminalError("cancelled", 409)
            if isinstance(do_progress_response, PyDoProgressReadFromInput):
                # We need to read from input
                chunk = await self.receive()
                if chunk.get('body', None) is not None:
                    assert isinstance(chunk['body'], bytes)
                    self.vm.notify_input(chunk['body'])
                if not chunk.get('more_body', False):
                    self.vm.notify_input_closed()
                continue
            if isinstance(do_progress_response, PyDoProgressExecuteRun):
                await self.run_coros_to_execute[do_progress_response.handle]
                await self.take_and_send_output()


    def get(self, name: str, serde: Serde[T] = JsonSerde()) -> typing.Awaitable[Optional[Any]]:
        coro = self.create_poll_or_cancel_coroutine(self.vm.sys_get_state(name))

        async def await_point():
            """Wait for this handle to be resolved."""
            res = await coro
            if res is None:
                return None
            return serde.deserialize(res)

        return await_point() # do not await here, the caller will do it.

    def state_keys(self) -> Awaitable[List[str]]:
        return self.create_poll_or_cancel_coroutine(self.vm.sys_get_state_keys()) # type: ignore

    def set(self, name: str, value: T, serde: Serde[T] = JsonSerde()) -> None:
        """Set the value associated with the given name."""
        buffer = serde.serialize(value)
        self.vm.sys_set_state(name, bytes(buffer))

    def clear(self, name: str) -> None:
        self.vm.sys_clear_state(name)

    def clear_all(self) -> None:
        self.vm.sys_clear_all_state()

    def request(self) -> Request:
        return Request(
            id=self.invocation.invocation_id,
            headers=dict(self.invocation.headers),
            attempt_headers=self.attempt_headers,
            body=self.invocation.input_buffer,
        )

    async def create_run_coroutine(self,
                                   handle: int,
                                   action: Callable[[], T] | Callable[[], Awaitable[T]],
                                   serde: Serde[T],
                                   max_attempts: Optional[int] = None,
                                   max_retry_duration: Optional[timedelta] = None):
        """Create a coroutine to poll the handle."""
        try:
            if inspect.iscoroutinefunction(action):
                action_result = await action() # type: ignore
            else:
                action_result = await asyncio.to_thread(action)

            buffer = serde.serialize(action_result)
            self.vm.propose_run_completion_success(handle, buffer)
        except TerminalError as t:
            failure = Failure(code=t.status_code, message=t.message)
            self.vm.propose_run_completion_failure(handle, failure)
        # pylint: disable=W0718
        except Exception as e:
            if max_attempts is None and max_retry_duration is None:
                # no retry policy
                raise e
            failure = Failure(code=500, message=str(e))
            max_duration_ms = None if max_retry_duration is None else int(max_retry_duration.total_seconds() * 1000)
            config = RunRetryConfig(max_attempts=max_attempts, max_duration=max_duration_ms)
            self.vm.propose_run_completion_transient(handle, failure=failure, attempt_duration_ms=1, config=config)

    # pylint: disable=W0236
    # pylint: disable=R0914
    def run(self,
                  name: str,
                  action: Callable[[], T] | Callable[[], Awaitable[T]],
                  serde: Optional[Serde[T]] = JsonSerde(),
                  max_attempts: Optional[int] = None,
                  max_retry_duration: Optional[timedelta] = None) -> Awaitable[T]:
        assert serde is not None
        handle = self.vm.sys_run(name)

        # Register closure to run
        self.run_coros_to_execute[handle] = self.create_run_coroutine(handle, action, serde, max_attempts, max_retry_duration)

        # Prepare response coroutine
        coro = self.create_poll_or_cancel_coroutine(handle)
        async def await_point():
            """Wait for this handle to be resolved."""
            res = await coro
            if res is None:
                return None
            return serde.deserialize(res)

        return await_point() # do not await here, the caller will do it.


    def sleep(self, delta: timedelta) -> Awaitable[None]:
        # convert timedelta to milliseconds
        millis = int(delta.total_seconds() * 1000)
        return self.create_poll_or_cancel_coroutine(self.vm.sys_sleep(millis)) # type: ignore

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
            res = await s.create_poll_or_cancel_coroutine(h)
            return o.deserialize(res) # type: ignore

        return await_point(self, handle.result_handle, output_serde)

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
        coro = self.create_poll_or_cancel_coroutine(handle)

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

    def cancel(self, invocation_id: str):
        """cancel an existing invocation by id."""
        if invocation_id is None:
            raise ValueError("invocation_id cannot be None")
        self.vm.sys_cancel(invocation_id)
