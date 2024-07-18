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
from typing import Any, Callable, Awaitable, Generic, Literal, Optional, TypeVar

from restate.serde import Serde

I = TypeVar('I')
O = TypeVar('O')

# we will use this symbol to store the handler in the function
RESTATE_UNIQUE_HANDLER_SYMBOL = str(object())

@dataclass
class ServiceTag:
    """
    This class is used to identify the service.
    """
    kind: Literal["object", "service", "workflow"]
    name: str

@dataclass
class HandlerIO(Generic[I, O]):
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
    input_serde: Serde[I]
    output_serde: Serde[O]

@dataclass
class Handler(Generic[I, O]):
    """
    Represents a handler for a service.
    """
    service_tag: ServiceTag
    handler_io: HandlerIO[I, O]
    kind: Optional[Literal["exclusive", "shared", "workflow"]]
    name: str
    fn: Callable[[Any, I], Awaitable[O]] | Callable[[Any], Awaitable[O]]
    arity: int


# disable too many arguments warning
# pylint: disable=R0913

def make_handler(service_tag: ServiceTag,
                 handler_io: HandlerIO[I, O],
                 name: str | None,
                 kind: Optional[Literal["exclusive", "shared", "workflow"]],
                 wrapped: Any,
                 arity: int) -> Handler[I, O]:
    """
    Factory function to create a handler.
    """
    # try to deduce the handler name
    handler_name = name
    if not handler_name:
        handler_name = wrapped.__name__
    if not handler_name:
        raise ValueError("Handler name must be provided")

    handler = Handler[I, O](service_tag,
                            handler_io,
                            kind,
                            handler_name,
                            wrapped,
                            arity)
    vars(wrapped)[RESTATE_UNIQUE_HANDLER_SYMBOL] = handler
    return handler

def handler_from_callable(wrapper: Callable[[Any, I], Awaitable[O]]) -> Handler[I, O]:
    """
    Get the handler from the callable.
    """
    try:
        return vars(wrapper)[RESTATE_UNIQUE_HANDLER_SYMBOL]
    except KeyError:
        raise ValueError("Handler not found") # pylint: disable=raise-missing-from

async def invoke_handler(handler: Handler[I, O], ctx: Any, in_buffer: bytes) -> bytes:
    """
    Invoke the handler with the given context and input.
    """
    if handler.arity == 2:
        in_arg = handler.handler_io.input_serde.deserialize(in_buffer) # type: ignore
        out_arg = await handler.fn(ctx, in_arg) # type: ignore [call-arg, arg-type]
    else:
        out_arg = await handler.fn(ctx) # type: ignore [call-arg]
    out_buffer = handler.handler_io.output_serde.serialize(out_arg) # type: ignore
    return bytes(out_buffer)
