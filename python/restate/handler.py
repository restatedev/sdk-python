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
"""
This module contains the definition of the Handler class,
which is used to define the handlers for the services.
"""

from dataclasses import dataclass
from inspect import Signature
from typing import Any, Callable, Awaitable, Generic, Literal, Optional, TypeVar

from restate.exceptions import TerminalError
from restate.serde import JsonSerde, Serde, PydanticJsonSerde

I = TypeVar('I')
O = TypeVar('O')

# we will use this symbol to store the handler in the function
RESTATE_UNIQUE_HANDLER_SYMBOL = str(object())


def try_import_pydantic_base_model():
    """
    Try to import PydanticBaseModel from Pydantic.
    """
    try:
        from pydantic import BaseModel # type: ignore # pylint: disable=import-outside-toplevel
        return BaseModel
    except ImportError:
        class Dummy: # pylint: disable=too-few-public-methods
            """a dummy class to use when Pydantic is not available"""

        return Dummy

PYDANTIC_BASE_MODEL = try_import_pydantic_base_model()

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
    """
    accept: str
    content_type: str
    input_serde: Serde[I]
    output_serde: Serde[O]
    pydantic_input_model: Optional[I] = None
    pydantic_output_model: Optional[O] = None

def is_pydantic(annotation) -> bool:
    """
    Check if an object is a Pydantic model.
    """
    try:
        return issubclass(annotation, PYDANTIC_BASE_MODEL)
    except TypeError:
        # annotation is not a class or a type
        return False


def infer_pydantic_io(handler_io: HandlerIO[I, O], signature: Signature):
    """
    Augment handler_io with Pydantic models when these are provided.
    This method will inspect the signature of an handler and will look for
    the input and the return types of a function, and will:
    * capture any Pydantic models (to be used later at discovery)
    * replace the default json serializer (is unchanged by a user) with a Pydantic serde
    """
    # check if the handlers I/O is a PydanticBaseModel
    annotation = list(signature.parameters.values())[-1].annotation
    if is_pydantic(annotation):
        handler_io.pydantic_input_model = annotation
        if isinstance(handler_io.input_serde, JsonSerde): # type: ignore
            handler_io.input_serde = PydanticJsonSerde(annotation)

    annotation = signature.return_annotation
    if is_pydantic(annotation):
        handler_io.pydantic_output_model = annotation
        if isinstance(handler_io.output_serde, JsonSerde): # type: ignore
            handler_io.output_serde = PydanticJsonSerde(annotation)

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
                 signature: Signature) -> Handler[I, O]:
    """
    Factory function to create a handler.
    """
    # try to deduce the handler name
    handler_name = name
    if not handler_name:
        handler_name = wrapped.__name__
    if not handler_name:
        raise ValueError("Handler name must be provided")

    if len(signature.parameters) == 0:
        raise ValueError("Handler must have at least one parameter")

    arity = len(signature.parameters)
    infer_pydantic_io(handler_io, signature)

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
        try:
            in_arg = handler.handler_io.input_serde.deserialize(in_buffer) # type: ignore
        except Exception as e:
            raise TerminalError(message=f"Unable to parse an input argument. {e}") from e
        out_arg = await handler.fn(ctx, in_arg) # type: ignore [call-arg, arg-type]
    else:
        out_arg = await handler.fn(ctx) # type: ignore [call-arg]
    out_buffer = handler.handler_io.output_serde.serialize(out_arg) # type: ignore
    return bytes(out_buffer)
