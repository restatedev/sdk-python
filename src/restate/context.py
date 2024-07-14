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
"""
Restate Context
"""

import abc
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar, Union
import typing
from datetime import timedelta

T = TypeVar('T')
I = TypeVar('I')
O = TypeVar('O')

RunAction = Union[Callable[[], T], Callable[[], Awaitable[T]]]

class Serde(typing.Generic[T], abc.ABC):
    """serializer/deserializer interface."""

    @abc.abstractmethod
    def deserialize(self, buf: bytes) -> typing.Optional[T]:
        """
        Deserializes a bytearray to an object.
        """

    @abc.abstractmethod
    def serialize(self, obj: typing.Optional[T]) -> bytes:
        """
        Serializes an object to a bytearray.
        """

@dataclass
class Request:
    """
    Represents an ingress request.

    Attributes:
        id: The unique identifier of the request.
        headers: The headers of the request.
        attempt_headers: The attempt headers of the request.
        body (bytes): The body of the request.
    """
    id: str
    headers: Dict[str, str]
    attempt_headers: Dict[str,str]
    body: bytes


class KeyValueStore(abc.ABC):
    """
    A key scoped key-value store.

    This class defines the interface for a key-value store,
    which allows storing and retrieving values
    based on a unique key.

    """

    @abc.abstractmethod
    def get(self, name: str) -> Awaitable[Optional[typing.Any]]:
        """
        Retrieves the value associated with the given name.
        """

    @abc.abstractmethod
    def state_keys(self) -> Awaitable[List[str]]:
        """Returns the list of keys in the store."""

    @abc.abstractmethod
    def set(self, name: str, value: T) -> None:
        """set the value associated with the given name."""

    @abc.abstractmethod
    def clear(self, name: str) -> None:
        """clear the value associated with the given name."""

    @abc.abstractmethod
    def clear_all(self) -> None:
        """clear all the values in the store."""


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
            serde: Optional[Serde[T]] = None) -> Awaitable[T | None]:
        """
        Runs the given action with the given name.
        """

    @abc.abstractmethod
    def sleep(self, delta: timedelta) -> Awaitable[None]:
        """
        Suspends the current invocation for the given duration
        """

    @abc.abstractmethod
    def service_call(self,
                     tpe: Callable[[Any, I], Awaitable[O]],
                     arg: I) -> Awaitable[O]:
        """
        Invokes the given service with the given argument.
        """


    @abc.abstractmethod
    def service_send(self,
                     tpe: Callable[[Any, I], Awaitable[O]],
                     arg: I,
                     send_delay: Optional[timedelta] = None,
                     ) -> None:
        """
        Invokes the given service with the given argument.
        """

    @abc.abstractmethod
    def object_call(self,
                    tpe: Callable[[Any, I], Awaitable[O]],
                    key: str,
                    arg: I) -> Awaitable[O]:
        """
        Invokes the given object with the given argument.
        """

    @abc.abstractmethod
    def object_send(self,
                    tpe: Callable[[Any, I], Awaitable[O]],
                    key: str,
                    arg: I,
                    send_delay: Optional[timedelta] = None,
                    ) -> None:
        """
        Send a message to an object with the given argument.
        """

    @abc.abstractmethod
    def workflow_call(self,
                    tpe: Callable[[Any, I], Awaitable[O]],
                    key: str,
                    arg: I) -> Awaitable[O]:
        """
        Invokes the given workflow with the given argument.
        """

    @abc.abstractmethod
    def workflow_send(self,
                    tpe: Callable[[Any, I], Awaitable[O]],
                    key: str,
                    arg: I,
                    send_delay: Optional[timedelta] = None,
                    ) -> None:
        """
        Send a message to an object with the given argument.
        """

    @abc.abstractmethod
    def awakeable(self,
                  serde: Optional[Serde[T]] = None) -> typing.Tuple[str, Awaitable[Any]]:
        """
        Returns the name of the awakeable and the future to be awaited.
        """

    @abc.abstractmethod
    def resolve_awakeable(self,
                          name: str,
                          value: I,
                          serde: typing.Optional[Serde[I]] = None) -> None:
        """
        Resolves the awakeable with the given name.
        """

    @abc.abstractmethod
    def reject_awakeable(self, name: str, failure_message: str, failure_code: int = 500) -> None:
        """
        Rejects the awakeable with the given name.
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
    def get(self, name: str) -> Awaitable[Optional[typing.Any]]:
        """
        Retrieves the value associated with the given name.
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

    def __init__(self, name: str, serde: typing.Optional[Serde[T]] = None) -> None:
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
    def value(self) -> Awaitable[T]:
        """
        Returns the value of the promise if it is resolved, None otherwise.
        """

class WorkflowContext(ObjectContext):
    """
    Represents the context of the current workflow invocation.
    """

    @abc.abstractmethod
    def promise(self, name: str) -> DurablePromise[Any]:
        """
        Returns a durable promise with the given name.
        """

class WorkflowSharedContext(ObjectSharedContext):
    """
    Represents the context of the current workflow invocation.
    """

    @abc.abstractmethod
    def promise(self, name: str) -> DurablePromise[Any]:
        """
        Returns a durable promise with the given name.
        """
