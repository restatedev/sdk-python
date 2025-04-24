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
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar, Union
import typing
from datetime import timedelta
from restate.serde import DefaultSerde, Serde

T = TypeVar('T')
I = TypeVar('I')
O = TypeVar('O')

RunAction = Union[Callable[..., Awaitable[T]], Callable[..., T]]


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
    safe to cleanup any attempt related resources, such as pending ctx.run() 3rd party calls, or any other resources that
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
    """
    id: str
    headers: Dict[str, str]
    attempt_headers: Dict[str,str]
    body: bytes
    attempt_finished_event: AttemptFinishedEvent


class KeyValueStore(abc.ABC):
    """
    A key scoped key-value store.

    This class defines the interface for a key-value store,
    which allows storing and retrieving values
    based on a unique key.

    """

    @abc.abstractmethod
    def get(self,
            name: str,
            serde: Serde[T] = DefaultSerde(),
            type_hint: Optional[typing.Type[T]] = None
            ) -> Awaitable[Optional[Any]]:
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
    def set(self,
            name: str,
            value: T,
            serde: Serde[T] = DefaultSerde()) -> None:
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
    def run(self,
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
    def sleep(self, delta: timedelta) -> RestateDurableSleepFuture:
        """
        Suspends the current invocation for the given duration
        """

    @abc.abstractmethod
    def service_call(self,
                     tpe: Callable[[Any, I], Awaitable[O]],
                     arg: I,
                     idempotency_key: str | None = None) -> RestateDurableCallFuture[O]:
        """
        Invokes the given service with the given argument.
        """


    @abc.abstractmethod
    def service_send(self,
                     tpe: Callable[[Any, I], Awaitable[O]],
                     arg: I,
                     send_delay: Optional[timedelta] = None,
                     idempotency_key: str | None = None,
                     ) -> SendHandle:
        """
        Invokes the given service with the given argument.
        """

    @abc.abstractmethod
    def object_call(self,
                    tpe: Callable[[Any, I], Awaitable[O]],
                    key: str,
                    arg: I,
                    idempotency_key: str | None = None,
                    ) -> RestateDurableCallFuture[O]:
        """
        Invokes the given object with the given argument.
        """

    @abc.abstractmethod
    def object_send(self,
                    tpe: Callable[[Any, I], Awaitable[O]],
                    key: str,
                    arg: I,
                    send_delay: Optional[timedelta] = None,
                    idempotency_key: str | None = None,
                    ) -> SendHandle:
        """
        Send a message to an object with the given argument.
        """

    @abc.abstractmethod
    def workflow_call(self,
                    tpe: Callable[[Any, I], Awaitable[O]],
                    key: str,
                    arg: I,
                    idempotency_key: str | None = None,
                    ) -> RestateDurableCallFuture[O]:
        """
        Invokes the given workflow with the given argument.
        """

    @abc.abstractmethod
    def workflow_send(self,
                    tpe: Callable[[Any, I], Awaitable[O]],
                    key: str,
                    arg: I,
                    send_delay: Optional[timedelta] = None,
                    idempotency_key: str | None = None,
                    ) -> SendHandle:
        """
        Send a message to an object with the given argument.
        """

    # pylint: disable=R0913
    @abc.abstractmethod
    def generic_call(self,
                     service: str,
                     handler: str,
                     arg: bytes,
                     key: Optional[str] = None,
                     idempotency_key: str | None = None)  -> RestateDurableCallFuture[bytes]:
        """
        Invokes the given generic service/handler with the given argument.
        """

    @abc.abstractmethod
    def generic_send(self,
                     service: str,
                     handler: str,
                     arg: bytes,
                     key: Optional[str] = None,
                     send_delay: Optional[timedelta] = None,
                     idempotency_key: str | None = None,
                    ) -> SendHandle:
        """
        Send a message to a generic service/handler with the given argument.
        """

    @abc.abstractmethod
    def awakeable(self,
                  serde: Serde[T] = DefaultSerde(),
                  type_hint: Optional[typing.Type[T]] = None
                  ) -> typing.Tuple[str, RestateDurableFuture[Any]]:
        """
        Returns the name of the awakeable and the future to be awaited.
        """

    @abc.abstractmethod
    def resolve_awakeable(self,
                          name: str,
                          value: I,
                          serde: Serde[I] = DefaultSerde()) -> None:
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
    def attach_invocation(self, invocation_id: str, serde: Serde[T] = DefaultSerde(),
                          type_hint: typing.Optional[typing.Type[T]] = None
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
    def get(self,
            name: str,
            serde: Serde[T] = DefaultSerde(),
            type_hint: Optional[typing.Type[T]] = None
            ) -> RestateDurableFuture[Optional[Any]]:
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

class WorkflowContext(ObjectContext):
    """
    Represents the context of the current workflow invocation.
    """

    @abc.abstractmethod
    def promise(self, name: str, serde: Serde[T] = DefaultSerde()) -> DurablePromise[Any]:
        """
        Returns a durable promise with the given name.
        """

class WorkflowSharedContext(ObjectSharedContext):
    """
    Represents the context of the current workflow invocation.
    """

    @abc.abstractmethod
    def promise(self, name: str, serde: Serde[T] = DefaultSerde()) -> DurablePromise[Any]:
        """
        Returns a durable promise with the given name.
        """
