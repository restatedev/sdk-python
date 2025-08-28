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
# pylint: disable=R0913
# pylint: disable=C0301
# pylint: disable=R0903
# pylint: disable=W0511

"""This module contains the restate context implementation based on the server"""

import asyncio
import contextvars
from random import Random
from datetime import timedelta
import inspect
import functools
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar, Union, Coroutine
import typing
import traceback
from uuid import UUID
import time

from restate.context import DurablePromise, AttemptFinishedEvent, HandlerType, ObjectContext, Request, RestateDurableCallFuture, RestateDurableFuture, RunAction, SendHandle, RestateDurableSleepFuture, RunOptions, P
from restate.exceptions import TerminalError
from restate.handler import Handler, handler_from_callable, invoke_handler
from restate.serde import BytesSerde, DefaultSerde, JsonSerde, Serde
from restate.server_types import ReceiveChannel, Send
from restate.vm import Failure, Invocation, NotReady, SuspendedException, VMWrapper, RunRetryConfig # pylint: disable=line-too-long
from restate.vm import DoProgressAnyCompleted, DoProgressCancelSignalReceived, DoProgressReadFromInput, DoProgressExecuteRun, DoWaitPendingRun
import typing_extensions


T = TypeVar('T')
I = TypeVar('I')
O = TypeVar('O')

class DisconnectedException(Exception):
    """
    This exception is raised when the connection to the restate server is lost.
    This can be due to a network error or a long inactivity timeout.
    The restate-server will automatically retry the attempt.
    """

    def __init__(self) -> None:
        super().__init__("Disconnected. The connection to the restate server was lost. Restate will retry the attempt.")


class ServerTeardownEvent(AttemptFinishedEvent):
    """
    This class implements the teardown event for the server.
    """

    def __init__(self, event: asyncio.Event) -> None:
        super().__init__()
        self.event = event

    def is_set(self):
        return self.event.is_set()

    async def wait(self):
        """Wait for the event to be set."""
        await self.event.wait()


class LazyFuture(typing.Generic[T]):
    """
    Creates a task lazily, and allows multiple awaiters to the same coroutine.
    The async_def will be executed at most 1 times. (0 if __await__ or get() not called) 
    """
    __slots__ = ['async_def', 'task']

    def __init__(self, async_def: Callable[[], typing.Coroutine[Any, Any, T]]) -> None:
        assert async_def is not None
        self.async_def = async_def
        self.task: asyncio.Task[T] | None = None

    def done(self):
        """
        check if completed
        """
        return self.task is not None and self.task.done()

    async def get(self) -> T:
        """Get the value of the future."""
        if self.task is None:
            self.task = asyncio.create_task(self.async_def())
        return await self.task

    def __await__(self):
        return self.get().__await__()

class ServerDurableFuture(RestateDurableFuture[T]):
    """This class implements a durable future API"""

    def __init__(self, context: "ServerInvocationContext", handle: int, async_def) -> None:
        super().__init__()
        self.context = context
        self.handle = handle
        self.future = LazyFuture(async_def)

    def is_completed(self):
        """
        A future is completed, either it was physically completed and its value has been collected.
        OR it might not yet physically completed (i.e. the async_def didn't finish yet) BUT our VM 
        already has a completion value for it.
        """
        return self.future.done() or self.context.vm.is_completed(self.handle)

    def __await__(self):
        return self.future.__await__()

class ServerDurableSleepFuture(RestateDurableSleepFuture, ServerDurableFuture[None]):
    """This class implements a durable sleep future API"""

    def __await__(self) -> typing.Generator[Any, Any, None]:
        return self.future.__await__()

class ServerCallDurableFuture(RestateDurableCallFuture[T], ServerDurableFuture[T]):
    """This class implements a durable future but for calls"""

    def __init__(self,
                 context: "ServerInvocationContext",
                 result_handle: int,
                 result_async_def,
                 invocation_id_async_def) -> None:
        super().__init__(context, result_handle, result_async_def)
        self.invocation_id_future = LazyFuture(invocation_id_async_def)

    async def invocation_id(self) -> str:
        """Get the invocation id."""
        return await self.invocation_id_future.get()

    async def cancel_invocation(self) -> None:
        """
        Cancels the invocation.

        Just a utility shortcut to:
        .. code-block:: python

           ctx.cancel_invocation(await f.invocation_id())
        """
        inv = await self.invocation_id()
        self.context.cancel_invocation(inv)

class ServerSendHandle(SendHandle):
    """This class implements the send API"""

    def __init__(self, context: "ServerInvocationContext", handle: int) -> None:
        super().__init__()
        self.context = context

        async def coro() -> str:
            if not context.vm.is_completed(handle):
                await context.create_poll_or_cancel_coroutine([handle])
            invocation_id = context.must_take_notification(handle)
            return typing.cast(str, invocation_id)

        self.future = LazyFuture(coro)

    async def invocation_id(self) -> str:
        """Get the invocation id."""
        return await self.future

    async def cancel_invocation(self) -> None:
        """Cancel the invocation."""
        invocation_id = await self.invocation_id()
        self.context.cancel_invocation(invocation_id)

async def async_value(n: Callable[[], T]) -> T:
    """convert a simple value to a coroutine."""
    return n()

class ServerDurablePromise(DurablePromise):
    """This class implements a durable promise API"""

    def __init__(self, server_context: "ServerInvocationContext", name, serde) -> None:
        super().__init__(name=name, serde=DefaultSerde() if serde is None else serde)
        self.server_context = server_context

    def value(self) -> RestateDurableFuture[Any]:
        handle = self.server_context.vm.sys_get_promise(self.name)
        update_restate_context_is_replaying(self.server_context.vm)
        return self.server_context.create_future(handle, self.serde)

    def resolve(self, value: Any) -> Awaitable[None]:
        vm: VMWrapper = self.server_context.vm
        assert self.serde is not None
        value_buffer = self.serde.serialize(value)
        handle = vm.sys_complete_promise_success(self.name, value_buffer)
        update_restate_context_is_replaying(self.server_context.vm)

        async def await_point():
            if not self.server_context.vm.is_completed(handle):
                await self.server_context.create_poll_or_cancel_coroutine([handle])
            self.server_context.must_take_notification(handle)

        return ServerDurableFuture(self.server_context, handle, await_point)

    def reject(self, message: str, code: int = 500) -> Awaitable[None]:
        vm: VMWrapper = self.server_context.vm
        py_failure = Failure(code=code, message=message)
        handle = vm.sys_complete_promise_failure(self.name, py_failure)
        update_restate_context_is_replaying(self.server_context.vm)

        async def await_point():
            if not self.server_context.vm.is_completed(handle):
                await self.server_context.create_poll_or_cancel_coroutine([handle])
            self.server_context.must_take_notification(handle)

        return ServerDurableFuture(self.server_context, handle, await_point)

    def peek(self) -> Awaitable[Any | None]:
        vm: VMWrapper = self.server_context.vm
        handle = vm.sys_peek_promise(self.name)
        update_restate_context_is_replaying(self.server_context.vm)
        serde = self.serde
        assert serde is not None
        return self.server_context.create_future(handle, serde)

    def __await__(self):
        return self.value().__await__()


# disable too many public method
# pylint: disable=R0904

class Tasks:
    """
    This class implements a list of tasks.
    """

    def __init__(self) -> None:
        self.tasks: set[asyncio.Future] = set()

    def add(self, task: asyncio.Future):
        """Add a task to the list."""
        self.tasks.add(task)

        def safe_remove(_):
            """Remove the task from the list."""
            try:
                self.tasks.remove(task)
            except KeyError:
                pass
            try:
                _ = task.result()
            # pylint: disable=W0718
            except Exception:
                pass
            except asyncio.CancelledError:
                pass

        task.add_done_callback(safe_remove)

    def cancel(self):
        """Cancel all tasks in the list."""
        to_cancel = list(self.tasks)
        self.tasks.clear()
        for task in to_cancel:
            task.cancel()

restate_context_is_replaying = contextvars.ContextVar('restate_context_is_replaying', default=False)

def update_restate_context_is_replaying(vm: VMWrapper):
    """Update the context var 'restate_context_is_replaying'. This should be called after each vm.sys_*"""
    restate_context_is_replaying.set(vm.is_replaying())

# pylint: disable=R0902
class ServerInvocationContext(ObjectContext):
    """This class implements the context for the restate framework based on the server."""

    def __init__(self,
                 vm: VMWrapper,
                 handler: Handler[I, O],
                 invocation: Invocation,
                 attempt_headers: Dict[str, str],
                 send: Send,
                 receive: ReceiveChannel
                 ) -> None:
        super().__init__()
        self.vm = vm
        self.handler = handler
        self.invocation = invocation
        self.attempt_headers = attempt_headers
        self.send = send
        self.random_instance = Random(invocation.random_seed)
        self.receive = receive
        self.run_coros_to_execute: dict[int,  Callable[[], Awaitable[None]]] = {}
        self.request_finished_event = asyncio.Event()
        self.tasks = Tasks()

    async def enter(self):
        """Invoke the user code."""
        update_restate_context_is_replaying(self.vm)
        try:
            in_buffer = self.invocation.input_buffer
            out_buffer = await invoke_handler(handler=self.handler, ctx=self, in_buffer=in_buffer)
            restate_context_is_replaying.set(False)
            self.vm.sys_write_output_success(bytes(out_buffer))
            self.vm.sys_end()
        except TerminalError as t:
            failure = Failure(code=t.status_code, message=t.message)
            restate_context_is_replaying.set(False)
            self.vm.sys_write_output_failure(failure)
            self.vm.sys_end()
        # pylint: disable=W0718
        except SuspendedException:
            pass
        except DisconnectedException:
            raise
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
        await self.receive.block_until_http_input_closed()
        # finally, we close our side
        # it is important to do it, after the other side has closed his side,
        # because some asgi servers (like hypercorn) will remove the stream
        # as soon as they see a close event (in asgi terms more_body=False)
        await self.send({
            'type': 'http.response.body',
            'body': b'',
            'more_body': False,
        })
        # notify to any holder of the abort signal that we are done

    def on_attempt_finished(self):
        """Notify the attempt finished event."""
        restate_context_is_replaying.set(False)
        self.request_finished_event.set()
        try:
            self.tasks.cancel()
        except asyncio.CancelledError:
            # ignore the cancelled error
            pass

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


    async def create_poll_or_cancel_coroutine(self, handles: typing.List[int]) -> None:
        """Create a coroutine to poll the handle."""
        await self.take_and_send_output()
        while True:
            do_progress_response = self.vm.do_progress(handles)
            if isinstance(do_progress_response, DoProgressAnyCompleted):
                # One of the handles completed
                return
            if isinstance(do_progress_response, DoProgressCancelSignalReceived):
                raise TerminalError("cancelled", 409)
            if isinstance(do_progress_response, DoProgressExecuteRun):
                fn = self.run_coros_to_execute[do_progress_response.handle]
                del self.run_coros_to_execute[do_progress_response.handle]
                assert fn is not None

                async def wrapper(f):
                    await f()
                    await self.take_and_send_output()
                    await self.receive.enqueue_restate_event({ 'type' : 'restate.run_completed', 'data': None})

                task = asyncio.create_task(wrapper(fn))
                self.tasks.add(task)
                continue
            if isinstance(do_progress_response, (DoWaitPendingRun, DoProgressReadFromInput)):
                chunk = await self.receive()
                if chunk.get('type') == 'restate.run_completed':
                    continue
                if chunk.get('type') == 'http.disconnect':
                    raise DisconnectedException()
                if chunk.get('body', None) is not None:
                    body = chunk.get('body')
                    assert isinstance(body, bytes)
                    self.vm.notify_input(body)
                if not chunk.get('more_body', False):
                    self.vm.notify_input_closed()

    def _create_fetch_result_coroutine(self, handle: int, serde: Serde[T] | None = None):
        """Create a coroutine that fetches a result from a notification handle."""
        async def fetch_result():
            if not self.vm.is_completed(handle):
                await self.create_poll_or_cancel_coroutine([handle])
            res = self.must_take_notification(handle)
            if res is None or serde is None:
                return res
            if isinstance(res, bytes):
                return serde.deserialize(res)
            return res

        return fetch_result

    def create_future(self, handle: int, serde: Serde[T] | None = None) -> ServerDurableFuture[T]:
        """Create a durable future for handling asynchronous state operations."""
        return ServerDurableFuture(self, handle, self._create_fetch_result_coroutine(handle, serde))

    def create_sleep_future(self, handle: int) -> ServerDurableSleepFuture:
        """Create a durable sleep future."""
        async def transform():
            if not self.vm.is_completed(handle):
                await self.create_poll_or_cancel_coroutine([handle])
            self.must_take_notification(handle)
        return ServerDurableSleepFuture(self, handle, transform)

    def create_call_future(self, handle: int, invocation_id_handle: int, serde: Serde[T] | None = None) -> ServerCallDurableFuture[T]:
        """Create a durable future."""
        async def inv_id_factory():
            if not self.vm.is_completed(invocation_id_handle):
                await self.create_poll_or_cancel_coroutine([invocation_id_handle])
            return self.must_take_notification(invocation_id_handle)

        return ServerCallDurableFuture(self, handle, self._create_fetch_result_coroutine(handle, serde), inv_id_factory)

    def get(self, name: str,
            serde: Serde[T] = DefaultSerde(),
            type_hint: Optional[typing.Type[T]] = None
            ) -> Awaitable[Optional[T]]:
        handle = self.vm.sys_get_state(name)
        update_restate_context_is_replaying(self.vm)
        if isinstance(serde, DefaultSerde):
            serde = serde.with_maybe_type(type_hint)
        return self.create_future(handle, serde) # type: ignore

    def state_keys(self) -> Awaitable[List[str]]:
        handle = self.vm.sys_get_state_keys()
        update_restate_context_is_replaying(self.vm)
        return self.create_future(handle)

    def set(self, name: str, value: T, serde: Serde[T] = DefaultSerde()) -> None:
        """Set the value associated with the given name."""
        if isinstance(serde, DefaultSerde):
            serde = serde.with_maybe_type(type(value))
        buffer = serde.serialize(value)
        self.vm.sys_set_state(name, bytes(buffer))
        update_restate_context_is_replaying(self.vm)

    def clear(self, name: str) -> None:
        self.vm.sys_clear_state(name)
        update_restate_context_is_replaying(self.vm)

    def clear_all(self) -> None:
        self.vm.sys_clear_all_state()
        update_restate_context_is_replaying(self.vm)

    def request(self) -> Request:
        return Request(
            id=self.invocation.invocation_id,
            headers=dict(self.invocation.headers),
            attempt_headers=self.attempt_headers,
            body=self.invocation.input_buffer,
            attempt_finished_event=ServerTeardownEvent(self.request_finished_event),
        )

    def random(self) -> Random:
        return self.random_instance

    def uuid(self) -> UUID:
        return UUID(int=self.random_instance.getrandbits(128), version=4)

    def time(self) -> RestateDurableFuture[float]:
        return self.run_typed("timestamp", time.time)

    # pylint: disable=R0914
    async def create_run_coroutine(self,
                                   handle: int,
                                   action: RunAction[T],
                                   serde: Serde[T],
                                   max_attempts: Optional[int] = None,
                                   max_retry_duration: Optional[timedelta] = None,
                                   ):
        """Create a coroutine to poll the handle."""
        try:
            if inspect.iscoroutinefunction(action):
                action_result: T = await action() # type: ignore
            else:
                loop = asyncio.get_running_loop()
                ctx = contextvars.copy_context()
                func_call = functools.partial(ctx.run, action)
                action_result_future = loop.run_in_executor(None, func_call)
                self.tasks.add(action_result_future)
                action_result = typing.cast(T, await action_result_future)

            buffer = serde.serialize(action_result)
            self.vm.propose_run_completion_success(handle, buffer)
        except TerminalError as t:
            failure = Failure(code=t.status_code, message=t.message)
            self.vm.propose_run_completion_failure(handle, failure)
        except asyncio.CancelledError as e:
            raise e from None
        # pylint: disable=W0718
        except Exception as e:
            if max_attempts is None and max_retry_duration is None:
                # no retry policy
                # todo: log the error
                self.vm.notify_error(repr(e), traceback.format_exc())
            else:
                failure = Failure(code=500, message=str(e))
                max_duration_ms = None if max_retry_duration is None else int(max_retry_duration.total_seconds() * 1000)
                config = RunRetryConfig(max_attempts=max_attempts, max_duration=max_duration_ms)
                self.vm.propose_run_completion_transient(handle, failure=failure, attempt_duration_ms=1, config=config)
    # pylint: disable=W0236
    # pylint: disable=R0914
    @typing_extensions.deprecated("`run` is deprecated, use `run_typed` instead for better type safety")
    def run(self,
                  name: str,
                  action: RunAction[T],
                  serde: Serde[T] = DefaultSerde(),
                  max_attempts: Optional[int] = None,
                  max_retry_duration: Optional[timedelta] = None,
                  type_hint: Optional[typing.Type[T]] = None,
                  args: Optional[typing.Tuple[Any, ...]] = None
                  ) -> RestateDurableFuture[T]:

        if isinstance(serde, DefaultSerde):
            if type_hint is None:
                signature = inspect.signature(action, eval_str=True)
                type_hint = signature.return_annotation
            serde = serde.with_maybe_type(type_hint)

        handle = self.vm.sys_run(name)
        update_restate_context_is_replaying(self.vm)

        if args is not None:
            noargs_action = functools.partial(action, *args)
        else:
            # todo: we can also verify by looking at the signature that there are no missing parameters
            noargs_action = action # type: ignore
        self.run_coros_to_execute[handle] = lambda : self.create_run_coroutine(handle, noargs_action, serde, max_attempts, max_retry_duration)
        return self.create_future(handle, serde) # type: ignore

    def run_typed(
        self,
        name: str,
        action:  Union[Callable[P, T], Callable[P, Coroutine[Any, Any, T]]],
        options: RunOptions[T] = RunOptions(),
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> RestateDurableFuture[T]:
        if isinstance(options.serde, DefaultSerde):
            if options.type_hint is None:
                signature = inspect.signature(action, eval_str=True)
                options.type_hint = signature.return_annotation
            options.serde = options.serde.with_maybe_type(options.type_hint)
        handle = self.vm.sys_run(name)
        update_restate_context_is_replaying(self.vm)

        func = functools.partial(action, *args, **kwargs)
        self.run_coros_to_execute[handle] = lambda : self.create_run_coroutine(handle, func, options.serde, options.max_attempts, options.max_retry_duration)
        return self.create_future(handle, options.serde)

    def sleep(self, delta: timedelta) -> RestateDurableSleepFuture:
        # convert timedelta to milliseconds
        millis = int(delta.total_seconds() * 1000)
        handle = self.vm.sys_sleep(millis)
        update_restate_context_is_replaying(self.vm)
        return self.create_sleep_future(handle) # type: ignore

    def do_call(self,
                tpe: HandlerType[I, O],
                parameter: I,
                key: Optional[str] = None,
                send_delay: Optional[timedelta] = None,
                send: bool = False,
                idempotency_key: str | None = None,
                headers: typing.Dict[str,str] | None = None
                ) -> RestateDurableCallFuture[O] | SendHandle:
        """Make an RPC call to the given handler"""
        target_handler = handler_from_callable(tpe)
        service=target_handler.service_tag.name
        handler=target_handler.name
        input_serde = target_handler.handler_io.input_serde
        output_serde = target_handler.handler_io.output_serde
        return self.do_raw_call(service, handler, parameter, input_serde, output_serde, key, send_delay, send, idempotency_key, headers)


    def do_raw_call(self,
                 service: str,
                 handler:str,
                 input_param: I,
                 input_serde: Serde[I],
                 output_serde: Serde[O],
                 key: Optional[str] = None,
                 send_delay: Optional[timedelta] = None,
                 send: bool = False,
                 idempotency_key: str | None = None,
                 headers: typing.Dict[str, str] | None = None
                 ) -> RestateDurableCallFuture[O] | SendHandle:
        """Make an RPC call to the given handler"""
        parameter = input_serde.serialize(input_param)
        if headers is not None:
            headers_kvs = list(headers.items())
        else:
            headers_kvs = []
        if send_delay:
            ms = int(send_delay.total_seconds() * 1000)
            send_handle = self.vm.sys_send(service, handler, parameter, key, delay=ms, idempotency_key=idempotency_key, headers=headers_kvs)
            update_restate_context_is_replaying(self.vm)
            return ServerSendHandle(self, send_handle)
        if send:
            send_handle = self.vm.sys_send(service, handler, parameter, key, idempotency_key=idempotency_key, headers=headers_kvs)
            update_restate_context_is_replaying(self.vm)
            return ServerSendHandle(self, send_handle)

        handle = self.vm.sys_call(service=service,
                                  handler=handler,
                                  parameter=parameter,
                                  key=key,
                                  idempotency_key=idempotency_key,
                                  headers=headers_kvs)
        update_restate_context_is_replaying(self.vm)

        return self.create_call_future(handle=handle.result_handle,
                                   invocation_id_handle=handle.invocation_id_handle,
                                   serde=output_serde)

    def service_call(self,
                     tpe: HandlerType[I, O],
                     arg: I,
                     idempotency_key: str | None = None,
                     headers: typing.Dict[str, str] | None = None
                     ) -> RestateDurableCallFuture[O]:
        coro = self.do_call(tpe, arg, idempotency_key=idempotency_key, headers=headers)
        assert not isinstance(coro, SendHandle)
        return coro


    def service_send(self, tpe: HandlerType[I, O], arg: I, send_delay: timedelta | None = None, idempotency_key: str | None = None, headers: typing.Dict[str, str] | None = None) -> SendHandle:
        send = self.do_call(tpe=tpe, parameter=arg, send_delay=send_delay, send=True, idempotency_key=idempotency_key, headers=headers)
        assert isinstance(send, SendHandle)
        return send

    def object_call(self,
                    tpe: HandlerType[I, O],
                    key: str,
                    arg: I,
                    idempotency_key: str | None = None,
                    headers: typing.Dict[str, str] | None = None
                    ) -> RestateDurableCallFuture[O]:
        coro = self.do_call(tpe, arg, key, idempotency_key=idempotency_key, headers=headers)
        assert not isinstance(coro, SendHandle)
        return coro

    def object_send(self, tpe: HandlerType[I, O], key: str, arg: I, send_delay: timedelta | None = None, idempotency_key: str | None = None, headers: typing.Dict[str, str] | None = None) -> SendHandle:
        send = self.do_call(tpe=tpe, key=key, parameter=arg, send_delay=send_delay, send=True, idempotency_key=idempotency_key, headers=headers)
        assert isinstance(send, SendHandle)
        return send

    def workflow_call(self,
                        tpe: HandlerType[I, O],
                        key: str,
                        arg: I,
                        idempotency_key: str | None = None,
                        headers: typing.Dict[str, str] | None = None
                        ) -> RestateDurableCallFuture[O]:
        return self.object_call(tpe, key, arg, idempotency_key=idempotency_key, headers=headers)

    def workflow_send(self, tpe: HandlerType[I, O], key: str, arg: I, send_delay: timedelta | None = None, idempotency_key: str | None = None, headers: typing.Dict[str, str] | None = None) -> SendHandle:
        send = self.object_send(tpe, key, arg, send_delay, idempotency_key=idempotency_key, headers=headers)
        assert isinstance(send, SendHandle)
        return send

    def generic_call(self, service: str, handler: str, arg: bytes, key: str | None = None, idempotency_key: str | None = None, headers: typing.Dict[str, str] | None = None) -> RestateDurableCallFuture[bytes]:
        serde = BytesSerde()
        call_handle = self.do_raw_call(service=service,
                                handler=handler,
                                input_param=arg,
                                input_serde=serde,
                                output_serde=serde,
                                key=key,
                                idempotency_key=idempotency_key,
                                headers=headers)
        assert not isinstance(call_handle, SendHandle)
        return call_handle

    def generic_send(self, service: str, handler: str, arg: bytes, key: str | None = None, send_delay: timedelta | None = None, idempotency_key: str | None = None, headers: typing.Dict[str, str] | None = None) -> SendHandle:
        serde = BytesSerde()
        send_handle =  self.do_raw_call(service=service,
                                handler=handler,
                                input_param=arg,
                                input_serde=serde,
                                output_serde=serde,
                                key=key,
                                send_delay=send_delay,
                                send=True,
                                idempotency_key=idempotency_key,
                                headers=headers)
        assert isinstance(send_handle, SendHandle)
        return send_handle

    def awakeable(self,
                  serde: Serde[I] = DefaultSerde(),
                  type_hint: Optional[typing.Type[I]] = None
                  ) -> typing.Tuple[str, RestateDurableFuture[Any]]:
        if isinstance(serde, DefaultSerde):
            serde = serde.with_maybe_type(type_hint)
        name, handle = self.vm.sys_awakeable()
        update_restate_context_is_replaying(self.vm)
        return name, self.create_future(handle, serde)

    def resolve_awakeable(self,
                          name: str,
                          value: I,
                          serde: Serde[I] = DefaultSerde()) -> None:
        if isinstance(serde, DefaultSerde):
            serde = serde.with_maybe_type(type(value))
        buf = serde.serialize(value)
        self.vm.sys_resolve_awakeable(name, buf)
        update_restate_context_is_replaying(self.vm)

    def reject_awakeable(self, name: str, failure_message: str, failure_code: int = 500) -> None:
        self.vm.sys_reject_awakeable(name, Failure(code=failure_code, message=failure_message))
        update_restate_context_is_replaying(self.vm)

    def promise(self, name: str, serde: typing.Optional[Serde[T]] = JsonSerde(), type_hint: Optional[typing.Type[T]] = None) -> DurablePromise[T]:
        """Create a durable promise."""
        if isinstance(serde, DefaultSerde):
            serde = serde.with_maybe_type(type_hint)
        return ServerDurablePromise(self, name, serde)

    def key(self) -> str:
        return self.invocation.key

    def cancel_invocation(self, invocation_id: str):
        """cancel an existing invocation by id."""
        if invocation_id is None:
            raise ValueError("invocation_id cannot be None")
        self.vm.sys_cancel(invocation_id)
        update_restate_context_is_replaying(self.vm)

    def attach_invocation(self, invocation_id: str, serde: Serde[T] = DefaultSerde(),
                          type_hint: Optional[typing.Type[T]] = None
                          ) -> RestateDurableFuture[T]:
        if invocation_id is None:
            raise ValueError("invocation_id cannot be None")
        if isinstance(serde, DefaultSerde):
            serde = serde.with_maybe_type(type_hint)
        handle = self.vm.attach_invocation(invocation_id)
        update_restate_context_is_replaying(self.vm)
        return self.create_future(handle, serde)
