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

T = TypeVar('T')
I = TypeVar('I')
O = TypeVar('O')

RunAction = Union[Callable[[], T], Callable[[], Awaitable[T]]]

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
    id: bytearray
    headers: Dict[str, str]
    attempt_headers: Dict[str, Union[str, List[str], None]]
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
    def run_named(self, name: str, action: RunAction[T]) -> Awaitable[T]:
        """
        Runs the given action with the given name.
        """

    @abc.abstractmethod
    def sleep(self, millis: int) -> Awaitable[None]:
        """
        Suspends the current invocation for the given number of milliseconds.
        """

    @abc.abstractmethod
    def service_call(self, tpe: Callable[[Any, I], Awaitable[O]], arg: I) -> Awaitable[O]:
        """
        Invokes the given service with the given argument.
        """

    @abc.abstractmethod
    def object_call(self, tpe: Callable[[Any, I], Awaitable[O]], key: str, arg: I) -> Awaitable[O]:
        """
        Invokes the given object with the given argument.
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
