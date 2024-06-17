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
This module contains the definition of the Handler class,
which is used to define the handlers for the services.
"""

from dataclasses import dataclass
import typing

from restate.serde import DeserializerType, SerializerType

I = typing.TypeVar('I')
O = typing.TypeVar('O')

# we will use this symbol to store the handler in the function
RESTATE_UNIQUE_HANDLER_SYMBOL = object()

@dataclass
class ServiceTag:
    """
    This class is used to identify the service.
    """
    kind: typing.Literal["object", "service", "workflow"]
    name: str

@dataclass
class HandlerIO(typing.Generic[I, O]):
    """
    Represents the input/output configuration for a handler.

    Attributes:
        accept (str): The accept header value for the handler.
        content_type (str): The content type header value for the handler.
        serializer: The serializer function to convert output to bytes.
        deserializer: The deserializer function to convert input type to bytes.
    """
    accept: str
    content_type: str
    serializer: SerializerType[O]
    deserializer: DeserializerType[I]

@dataclass
class Handler(typing.Generic[I, O]):
    """
    Represents a handler for a service.
    """
    service_tag: ServiceTag
    handler_io: HandlerIO[I, O]
    kind: typing.Optional[typing.Literal["exclusive", "shared", "workflow"]]
    name: str
    fn: typing.Callable[[typing.Any, I], typing.Awaitable[O]]


def make_handler(service_tag: ServiceTag,
                      handler_io: HandlerIO[I, O],
                      name: str | None,
                      kind: typing.Optional[typing.Literal["exclusive", "shared"]],
                      wrapped: typing.Any) -> Handler[I, O]:
    """
    Factory function to create a handler.
    """
    # try to deduce the handler name
    handler_name = name
    if not handler_name:
        handler_name = wrapped.__name__
    if not handler_name:
        raise ValueError("Handler name must be provided")

    handler = Handler[I, O](service_tag, handler_io, kind, handler_name, wrapped)
    vars(wrapped)[RESTATE_UNIQUE_HANDLER_SYMBOL] = handler
    return handler
