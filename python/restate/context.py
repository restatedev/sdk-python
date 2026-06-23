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
# pylint: disable=R0913,C0301,R0917
"""
Restate Context
"""

import abc
from random import Random
from uuid import UUID
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar, Union, Coroutine, overload, ParamSpec
import typing
from datetime import timedelta

from restate.serde import DefaultSerde, Serde

T = TypeVar("T")
I = TypeVar("I")
O = TypeVar("O")
P = ParamSpec("P")

HandlerType = Union[Callable[[Any, I], Awaitable[O]], Callable[[Any], Awaitable[O]]]
RunAction = Union[Callable[..., Coroutine[Any, Any, T]], Callable[..., T]]


# pylint: disable=R0902
@dataclass
class RunOptions(typing.Generic[T]):
    """
    Options for running an action.
    """

    serde: Serde[T] = DefaultSerde()
    """The serialization/deserialization mechanism. - if the default serde is used, a default serializer will be used based on the type.
                    See also 'type_hint'."""
    type_hint: Optional[typing.Type[T]] = None
    """The type hint of the return value of the action. This is used to pick the serializer. If None, the type hint will be inferred from the action's return type, or the provided serializer."""
    max_attempts: Optional[int] = None
    """Max number of attempts (including the initial), before giving up.

    When giving up, `ctx.run` will throw a `TerminalError` wrapping the original error message."""
    max_duration: Optional[timedelta] = None
    """Max duration of retries, before giving up.

    When giving up, `ctx.run` will throw a `TerminalError` wrapping the original error message."""
    initial_retry_interval: Optional[timedelta] = None
    """Initial interval for the first retry attempt.
    Retry interval will grow by a factor specified in `retry_interval_factor`.

    If any of the other retry related fields is specified, the default for this field is 50 milliseconds, otherwise restate will fallback to the overall invocation retry policy."""
    max_retry_interval: Optional[timedelta] = None
    """Max interval between retries.
    Retry interval will grow by a factor specified in `retry_interval_factor`.

    The default is 10 seconds."""
    retry_interval_factor: Optional[float] = None
    """Exponentiation factor to use when computing the next retry delay.

    If any of the other retry related fields is specified, the default for this field is `2`, meaning retry interval will double at each attempt, otherwise restate will fallback to the overall invocation retry policy."""
    max_retry_duration: Optional[timedelta] = None
    """Deprecated: Use max_duration instead."""


# pylint: disable=R0903
class RestateDurableFuture(typing.Generic[T], Awaitable[T]):
    """
    Represents a durable future.
    """

    @abc.abstractmethod
    def __await__(self) -> typing.Generator[Any, Any, T]:
        pass


# pylint: disable=R0903
class RestateDurableCallFuture(RestateDurableFuture[T]):
    """
    Represents a durable call future.
    """

    @abc.abstractmethod
    async def invocation_id(self) -> str:
        """
        Returns the invocation id of the call.
        """

    @abc.abstractmethod
    async def cancel_invocation(self) -> None:
        """
        Cancels the invocation.

        Just a utility shortcut to:
        .. code-block:: python

            await ctx.cancel_invocation(await f.invocation_id())
        """


class RestateDurableSleepFuture(RestateDurableFuture[None]):
    """
    Represents a durable sleep future.
    """

    @abc.abstractmethod
    def __await__(self) -> typing.Generator[Any, Any, None]:
        pass


class AttemptFinishedEvent(abc.ABC):
    """
    Represents an attempt finished event.

    This event is used to signal that an attempt has finished (either successfully or with an error), and it is now
    safe to clean up any attempt related resources, such as pending ctx.run() 3rd party calls, or any other resources that
    are only valid for the duration of the attempt.

    An attempt is considered finished when either the connection to the restate server is closed, the invocation is completed, or a transient
    error occurs.
    """

    @abc.abstractmethod
    def is_set(self) -> bool:
        """
        Returns True if the event is set, False otherwise.
        """

    @abc.abstractmethod
    async def wait(self):
        """
        Waits for the event to be set.
        """


@dataclass
class Request:
    """
    Represents an ingress request.

    Attributes:
        id (str): The unique identifier of the request.
        headers (dict[str, str]): The headers of the request.
        attempt_headers (dict[str, str]): The attempt headers of the request.
        body (bytes): The body of the request.
        attempt_finished_event (AttemptFinishedEvent): The teardown event of the request.
        scope (Optional[str]): The scope key with which this invocation was submitted, if any.
        limit_key (Optional[str]): The limit key with which this invocation was submitted, if any.
        idempotency_key (Optional[str]): The idempotency key with which this invocation was submitted, if any.
    """

    id: str
    headers: Dict[str, str]
    attempt_headers: Dict[str, str]
    body: bytes
    attempt_finished_event: AttemptFinishedEvent
    scope: Optional[str] = None
    limit_key: Optional[str] = None
    idempotency_key: Optional[str] = None


class KeyValueStore(abc.ABC):
    """
    A key scoped key-value store.

    This class defines the interface for a key-value store,
    which allows storing and retrieving values
    based on a unique key.

    """

    @abc.abstractmethod
    def get(
        self, name: str, serde: Serde[T] = DefaultSerde(), type_hint: Optional[typing.Type[T]] = None
    ) -> Awaitable[Optional[T]]:
        """
        Retrieves the value associated with the given name.

        Args:
            name: The state name
            serde: The serialization/deserialization mechanism. - if the default serde is used, a default serializer will be used based on the type.
                    See also 'type_hint'.
            type_hint: The type hint of the return value. This is used to pick the serializer. If None, the type hint will be inferred from the action's return type, or the provided serializer.
        """

    @abc.abstractmethod
    def state_keys(self) -> Awaitable[List[str]]:
        """Returns the list of keys in the store."""

    @abc.abstractmethod
    def set(self, name: str, value: T, serde: Serde[T] = DefaultSerde()) -> None:
        """set the value associated with the given name."""

    @abc.abstractmethod
    def clear(self, name: str) -> None:
        """clear the value associated with the given name."""

    @abc.abstractmethod
    def clear_all(self) -> None:
        """clear all the values in the store."""


# pylint: disable=R0903
class SendHandle(abc.ABC):
    """
    Represents a send operation.
    """

    @abc.abstractmethod
    async def invocation_id(self) -> str:
        """
        Returns the invocation id of the send operation.
        """

    @abc.abstractmethod
    async def cancel_invocation(self) -> None:
        """
        Cancels the invocation.

        Just a utility shortcut to:
        .. code-block:: python

            await ctx.cancel_invocation(await f.invocation_id())
        """


class ScopedContext(abc.ABC):
    """
    A context for making RPC calls within a specific scope.

    **NOTE:** This API is in preview and is not enabled by default.
    To use it in restate-server 1.7, enable the flow control and protocol v7 experimental features,
    via ``RESTATE_EXPERIMENTAL_ENABLE_PROTOCOL_V7=true`` and ``RESTATE_EXPERIMENTAL_ENABLE_VQUEUES=true``.
    These can be enabled only on **new clusters**, for more info check out https://docs.restate.dev/services/flow-control#enabling-flow-control.
    When the experimental features are disabled, this method fails the invocation with a retryable error, causing the invocation to be retried until fixed.

    Returned by ``ctx.scope(scope_key)``: calls and sends made through this context
    carry the captured scope, and each method additionally accepts an optional
    ``limit_key``.

    The limit key enforces hierarchical concurrency limits on invocations sharing the same scope.
    It can have one or two levels separated by ``/`` (e.g. ``"tenant1"`` or ``"tenant1/user42"``).
    Each level must consist only of ``[a-zA-Z0-9_.-]`` characters, and 1 <= length <= 36.

    The limit key is **not** part of the request identity: two calls to the same target with the
    same scope and object key but different limit keys refer to the **same** resource instance.
    The limit key only affects concurrency limits, not resource identity.
    """

    @abc.abstractmethod
    def service_call(
        self,
        tpe: HandlerType[I, O],
        arg: I,
        limit_key: str | None = None,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> RestateDurableCallFuture[O]:
        """
        Invokes the given service with the given argument, within this scope.
        """

    @abc.abstractmethod
    def service_send(
        self,
        tpe: HandlerType[I, O],
        arg: I,
        send_delay: Optional[timedelta] = None,
        limit_key: str | None = None,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> SendHandle:
        """
        Invokes the given service with the given argument, within this scope.
        """

    @abc.abstractmethod
    def workflow_call(
        self,
        tpe: HandlerType[I, O],
        key: str,
        arg: I,
        limit_key: str | None = None,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> RestateDurableCallFuture[O]:
        """
        Invokes the given workflow with the given argument, within this scope.
        """

    @abc.abstractmethod
    def workflow_send(
        self,
        tpe: HandlerType[I, O],
        key: str,
        arg: I,
        send_delay: Optional[timedelta] = None,
        limit_key: str | None = None,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> SendHandle:
        """
        Send a message to a workflow with the given argument, within this scope.
        """


class Context(abc.ABC):
    """
    Represents the context of the current invocation.
    """

    @abc.abstractmethod
    def request(self) -> Request:
        """
        Returns the request object.
        """

    @abc.abstractmethod
    def scope(self, scope: str) -> ScopedContext:
        """
        Returns a ``ScopedContext`` that routes all outgoing calls within the given scope.

        **NOTE:** This API is in preview and is not enabled by default.
        To use it in restate-server 1.7, enable the flow control and protocol v7 experimental features,
        via ``RESTATE_EXPERIMENTAL_ENABLE_PROTOCOL_V7=true`` and ``RESTATE_EXPERIMENTAL_ENABLE_VQUEUES=true``.
        These can be enabled only on **new clusters**, for more info check out https://docs.restate.dev/services/flow-control#enabling-flow-control.
        If these experimental features aren't enabled, the call fails with a retryable error and keeps retrying until they are.

        A scope is a sub-grouping of resources (invocations, workflow instances, concurrency limits) within the Restate cluster.
        It becomes part of the target identity tuple:
        - ``scope, service, handler, idempotency_key?``
        - ``scope, workflow, workflow_key, handler``

        Under the hood, the scope contributes to the partition key, so all resources in a scope get co-located by the restate-server.

        Omitting the scope (i.e. using the regular ``service_call`` / ``workflow_call`` methods)
        is equivalent to calling with no scope, which is the existing behavior.

        The scope must consist only of ``[a-zA-Z0-9_.-]`` characters, with 1 <= length <= 36 chars.

        Args:
            scope: the scope identifier

        See also: https://docs.restate.dev/services/flow-control
        """

    @abc.abstractmethod
    def signal(
        self, name: str, serde: Serde[T] = DefaultSerde(), type_hint: Optional[typing.Type[T]] = None
    ) -> RestateDurableFuture[T]:
        """
        Awaits a named signal on the current invocation, resolving when the signal arrives.

        Args:
            name: The signal name.
            serde: The serialization/deserialization mechanism. Defaults to DefaultSerde.
            type_hint: The type hint of the signal value, used to pick the serializer.
        """

    @abc.abstractmethod
    def resolve_signal(self, invocation_id: str, name: str, value: I, serde: Serde[I] = DefaultSerde()) -> None:
        """
        Resolves a named signal on the target invocation with the given value.

        Args:
            invocation_id: The id of the target invocation.
            name: The signal name.
            value: The value to resolve the signal with.
            serde: The serialization mechanism. Defaults to DefaultSerde.
        """

    @abc.abstractmethod
    def reject_signal(self, invocation_id: str, name: str, failure_message: str, failure_code: int = 500) -> None:
        """
        Rejects a named signal on the target invocation. The handler awaiting the
        signal will observe a terminal error with the given message and code.

        Args:
            invocation_id: The id of the target invocation.
            name: The signal name.
            failure_message: The failure message.
            failure_code: The failure code. Defaults to 500.
        """

    @abc.abstractmethod
    def random(self) -> Random:
        """
        Returns a Random instance inherently predictable, deterministically seeded by Restate.

        This instance is useful to generate identifiers, idempotency keys, and for uniform sampling from a set of options.
        """

    @abc.abstractmethod
    def uuid(self) -> UUID:
        """
        Returns a random UUID, deterministically seeded.

        This UUID will be stable across retries and replays.
        """

    @abc.abstractmethod
    def time(self) -> RestateDurableFuture[float]:
        """
        Returns the result of time.time(), durably recorded in the journal.

        This timestamp will be stable across retries and replays.
        """

    @overload
    @abc.abstractmethod
    def run(
        self,
        name: str,
        action: Callable[..., Coroutine[Any, Any, T]],
        serde: Serde[T] = DefaultSerde(),
        max_attempts: typing.Optional[int] = None,
        max_retry_duration: typing.Optional[timedelta] = None,
        type_hint: Optional[typing.Type[T]] = None,
        args: Optional[typing.Tuple[Any, ...]] = None,
    ) -> RestateDurableFuture[T]:
        """
        Runs the given action with the given name.

        DEPRECATED: Use ctx.run_typed instead.

        Args:
            name: The name of the action.
            action: The action to run.
            serde: The serialization/deserialization mechanism. - if the default serde is used, a default serializer will be used based on the type.
                    See also 'type_hint'.
            max_attempts:   The maximum number of retry attempts to complete the action.
                            If None, the action will be retried indefinitely, until it succeeds.
                            Otherwise, the action will be retried until the maximum number of attempts is reached and then it will raise a TerminalError.
            max_retry_duration: The maximum duration for retrying. If None, the action will be retried indefinitely, until it succeeds.
                                Otherwise, the action will be retried until the maximum duration is reached and then it will raise a TerminalError.
            type_hint: The type hint of the return value of the action.
                        This is used to pick the serializer. If None, the type hint will be inferred from the action's return type, or the provided serializer.

        """

    @overload
    @abc.abstractmethod
    def run(
        self,
        name: str,
        action: Callable[..., T],
        serde: Serde[T] = DefaultSerde(),
        max_attempts: typing.Optional[int] = None,
        max_retry_duration: typing.Optional[timedelta] = None,
        type_hint: Optional[typing.Type[T]] = None,
        args: Optional[typing.Tuple[Any, ...]] = None,
    ) -> RestateDurableFuture[T]:
        """
        Runs the given coroutine action with the given name.

        DEPRECATED: Use ctx.run_typed instead.

        Args:
            name: The name of the action.
            action: The action to run.
            serde: The serialization/deserialization mechanism. - if the default serde is used, a default serializer will be used based on the type.
                    See also 'type_hint'.
            max_attempts:   The maximum number of retry attempts to complete the action.
                            If None, the action will be retried indefinitely, until it succeeds.
                            Otherwise, the action will be retried until the maximum number of attempts is reached and then it will raise a TerminalError.
            max_retry_duration: The maximum duration for retrying. If None, the action will be retried indefinitely, until it succeeds.
                                Otherwise, the action will be retried until the maximum duration is reached and then it will raise a TerminalError.
            type_hint: The type hint of the return value of the action.
                        This is used to pick the serializer. If None, the type hint will be inferred from the action's return type, or the provided serializer.

        """

    @abc.abstractmethod
    def run(
        self,
        name: str,
        action: RunAction[T],
        serde: Serde[T] = DefaultSerde(),
        max_attempts: typing.Optional[int] = None,
        max_retry_duration: typing.Optional[timedelta] = None,
        type_hint: Optional[typing.Type[T]] = None,
        args: Optional[typing.Tuple[Any, ...]] = None,
    ) -> RestateDurableFuture[T]:
        """
        Runs the given action with the given name.

        DEPRECATED: Use ctx.run_typed instead.

        Args:
            name: The name of the action.
            action: The action to run.
            serde: The serialization/deserialization mechanism. - if the default serde is used, a default serializer will be used based on the type.
                    See also 'type_hint'.
            max_attempts:   The maximum number of retry attempts to complete the action.
                            If None, the action will be retried indefinitely, until it succeeds.
                            Otherwise, the action will be retried until the maximum number of attempts is reached and then it will raise a TerminalError.
            max_retry_duration: The maximum duration for retrying. If None, the action will be retried indefinitely, until it succeeds.
                                Otherwise, the action will be retried until the maximum duration is reached and then it will raise a TerminalError.
            type_hint: The type hint of the return value of the action.
                        This is used to pick the serializer. If None, the type hint will be inferred from the action's return type, or the provided serializer.

        """

    @overload
    @abc.abstractmethod
    def run_typed(
        self,
        name: str,
        action: Callable[P, Coroutine[Any, Any, T]],
        options: RunOptions[T] = RunOptions(),
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> RestateDurableFuture[T]:
        """
        Typed version of run that provides type hints for the function arguments.
        Runs the given action with the given name.

        Args:
            name: The name of the action.
            action: The action to run.
            options: The options for the run.
            *args: The arguments to pass to the action.
            **kwargs: The keyword arguments to pass to the action.
        """

    @overload
    @abc.abstractmethod
    def run_typed(
        self,
        name: str,
        action: Callable[P, T],
        options: RunOptions[T] = RunOptions(),
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> RestateDurableFuture[T]:
        """
        Typed version of run that provides type hints for the function arguments.
        Runs the given coroutine action with the given name.

        Args:
            name: The name of the action.
            action: The action to run.
            options: The options for the run.
            *args: The arguments to pass to the action.
            **kwargs: The keyword arguments to pass to the action.
        """

    @abc.abstractmethod
    def run_typed(
        self,
        name: str,
        action: Union[Callable[P, Coroutine[Any, Any, T]], Callable[P, T]],
        options: RunOptions[T] = RunOptions(),
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> RestateDurableFuture[T]:
        """
        Typed version of run that provides type hints for the function arguments.
        Runs the given action with the given name.

        Args:
            name: The name of the action.
            action: The action to run.
            options: The options for the run.
            *args: The arguments to pass to the action.
            **kwargs: The keyword arguments to pass to the action.

        """

    @abc.abstractmethod
    def sleep(self, delta: timedelta, name: Optional[str] = None) -> RestateDurableSleepFuture:
        """
        Suspends the current invocation for the given duration
        """

    @abc.abstractmethod
    def service_call(
        self,
        tpe: HandlerType[I, O],
        arg: I,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> RestateDurableCallFuture[O]:
        """
        Invokes the given service with the given argument.
        """

    @abc.abstractmethod
    def service_send(
        self,
        tpe: HandlerType[I, O],
        arg: I,
        send_delay: Optional[timedelta] = None,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> SendHandle:
        """
        Invokes the given service with the given argument.
        """

    @abc.abstractmethod
    def object_call(
        self,
        tpe: HandlerType[I, O],
        key: str,
        arg: I,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> RestateDurableCallFuture[O]:
        """
        Invokes the given object with the given argument.
        """

    @abc.abstractmethod
    def object_send(
        self,
        tpe: HandlerType[I, O],
        key: str,
        arg: I,
        send_delay: Optional[timedelta] = None,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> SendHandle:
        """
        Send a message to an object with the given argument.
        """

    @abc.abstractmethod
    def workflow_call(
        self,
        tpe: HandlerType[I, O],
        key: str,
        arg: I,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> RestateDurableCallFuture[O]:
        """
        Invokes the given workflow with the given argument.
        """

    @abc.abstractmethod
    def workflow_send(
        self,
        tpe: HandlerType[I, O],
        key: str,
        arg: I,
        send_delay: Optional[timedelta] = None,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
    ) -> SendHandle:
        """
        Send a message to an object with the given argument.
        """

    # pylint: disable=R0913
    @abc.abstractmethod
    def generic_call(
        self,
        service: str,
        handler: str,
        arg: bytes,
        key: Optional[str] = None,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
        scope: str | None = None,
        limit_key: str | None = None,
    ) -> RestateDurableCallFuture[bytes]:
        """
        Invokes the given generic service/handler with the given argument.

        Args:
            scope: Optional scope to route the call within. See ``Context.scope``. Since restate-server 1.7.
            limit_key: Optional concurrency limit key within the scope. Requires ``scope`` to be set. Since restate-server 1.7.
        """

    @abc.abstractmethod
    def generic_send(
        self,
        service: str,
        handler: str,
        arg: bytes,
        key: Optional[str] = None,
        send_delay: Optional[timedelta] = None,
        idempotency_key: str | None = None,
        headers: typing.Dict[str, str] | None = None,
        scope: str | None = None,
        limit_key: str | None = None,
    ) -> SendHandle:
        """
        Send a message to a generic service/handler with the given argument.

        Args:
            scope: Optional scope to route the send within. See ``Context.scope``. Since restate-server 1.7.
            limit_key: Optional concurrency limit key within the scope. Requires ``scope`` to be set. Since restate-server 1.7.
        """

    @abc.abstractmethod
    def awakeable(
        self, serde: Serde[T] = DefaultSerde(), type_hint: Optional[typing.Type[T]] = None
    ) -> typing.Tuple[str, RestateDurableFuture[T]]:
        """
        Returns the name of the awakeable and the future to be awaited.
        """

    @abc.abstractmethod
    def resolve_awakeable(self, name: str, value: I, serde: Serde[I] = DefaultSerde()) -> None:
        """
        Resolves the awakeable with the given name.
        """

    @abc.abstractmethod
    def reject_awakeable(self, name: str, failure_message: str, failure_code: int = 500) -> None:
        """
        Rejects the awakeable with the given name.
        """

    @abc.abstractmethod
    def cancel_invocation(self, invocation_id: str):
        """
        Cancels the invocation with the given id.
        """

    @abc.abstractmethod
    def attach_invocation(
        self, invocation_id: str, serde: Serde[T] = DefaultSerde(), type_hint: typing.Optional[typing.Type[T]] = None
    ) -> RestateDurableFuture[T]:
        """
        Attaches the invocation with the given id.
        """


class ObjectContext(Context, KeyValueStore):
    """
    Represents the context of the current invocation.
    """

    @abc.abstractmethod
    def key(self) -> str:
        """
        Returns the key of the current object.
        """


class ObjectSharedContext(Context):
    """
    Represents the context of the current invocation.
    """

    @abc.abstractmethod
    def key(self) -> str:
        """Returns the key of the current object."""

    @abc.abstractmethod
    def get(
        self, name: str, serde: Serde[T] = DefaultSerde(), type_hint: Optional[typing.Type[T]] = None
    ) -> RestateDurableFuture[Optional[T]]:
        """
        Retrieves the value associated with the given name.

        Args:
            name: The state name
            serde: The serialization/deserialization mechanism. - if the default serde is used, a default serializer will be used based on the type.
                    See also 'type_hint'.
            type_hint: The type hint of the return value. This is used to pick the serializer. If None, the type hint will be inferred from the action's return type, or the provided serializer.
        """

    @abc.abstractmethod
    def state_keys(self) -> Awaitable[List[str]]:
        """
        Returns the list of keys in the store.
        """


class DurablePromise(typing.Generic[T]):
    """
    Represents a durable promise.
    """

    def __init__(self, name: str, serde: Serde[T] = DefaultSerde()) -> None:
        self.name = name
        self.serde = serde

    @abc.abstractmethod
    def resolve(self, value: T) -> Awaitable[None]:
        """
        Resolves the promise with the given value.
        """

    @abc.abstractmethod
    def reject(self, message: str, code: int = 500) -> Awaitable[None]:
        """
        Rejects the promise with the given message and code.
        """

    @abc.abstractmethod
    def peek(self) -> Awaitable[typing.Optional[T]]:
        """
        Returns the value of the promise if it is resolved, None otherwise.
        """

    @abc.abstractmethod
    def value(self) -> RestateDurableFuture[T]:
        """
        Returns the value of the promise if it is resolved, None otherwise.
        """

    @abc.abstractmethod
    def __await__(self) -> typing.Generator[Any, Any, T]:
        """
        Returns the value of the promise. This is a shortcut for calling value() and awaiting it.
        """


class WorkflowContext(ObjectContext):
    """
    Represents the context of the current workflow invocation.
    """

    @abc.abstractmethod
    def promise(
        self, name: str, serde: Serde[T] = DefaultSerde(), type_hint: Optional[typing.Type[T]] = None
    ) -> DurablePromise[T]:
        """
        Returns a durable promise with the given name.
        """


class WorkflowSharedContext(ObjectSharedContext):
    """
    Represents the context of the current workflow invocation.
    """

    @abc.abstractmethod
    def promise(
        self, name: str, serde: Serde[T] = DefaultSerde(), type_hint: Optional[typing.Type[T]] = None
    ) -> DurablePromise[T]:
        """
        Returns a durable promise with the given name.
        """
