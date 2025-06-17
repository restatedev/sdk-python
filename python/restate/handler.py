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
from datetime import timedelta
from inspect import Signature
from typing import Any, Callable, Awaitable, Dict, Generic, Literal, Optional, TypeVar

from restate.context import HandlerType
from restate.exceptions import TerminalError
from restate.serde import DefaultSerde, PydanticJsonSerde, Serde, is_pydantic

I = TypeVar('I')
O = TypeVar('O')
T = TypeVar('T')

# we will use this symbol to store the handler in the function
RESTATE_UNIQUE_HANDLER_SYMBOL = str(object())

@dataclass
class ServiceTag:
    """
    This class is used to identify the service.
    """
    kind: Literal["object", "service", "workflow"]
    name: str
    description: Optional[str] = None
    metadata: Optional[Dict[str, str]] = None

@dataclass
class TypeHint(Generic[T]):
    """
    Represents a type hint.
    """
    annotation: Optional[T] = None
    is_pydantic: bool = False
    is_void: bool = False

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
    input_type: Optional[TypeHint[I]] = None
    output_type: Optional[TypeHint[O]] = None


def update_handler_io_with_type_hints(handler_io: HandlerIO[I, O], signature: Signature):
    """
    Augment handler_io with additional information about the input and output types.

    This function has a special check for Pydantic models when these are provided.
    This method will inspect the signature of an handler and will look for
    the input and the return types of a function, and will:
    * capture any Pydantic models (to be used later at discovery)
    * replace the default json serializer (is unchanged by a user) with a Pydantic serde
    """
    params = list(signature.parameters.values())
    if len(params) == 1:
        # if there is only one parameter, it is the context.
        handler_io.input_type = TypeHint(is_void=True)
    else:
        annotation = params[-1].annotation
        handler_io.input_type = TypeHint(annotation=annotation, is_pydantic=False)
        if is_pydantic(annotation):
            handler_io.input_type.is_pydantic = True
            if isinstance(handler_io.input_serde, DefaultSerde):
                handler_io.input_serde = PydanticJsonSerde(annotation)

    annotation = signature.return_annotation
    if annotation is None or annotation is Signature.empty:
        # if there is no return annotation, we assume it is void
        handler_io.output_type = TypeHint(is_void=True)
    else:
        handler_io.output_type = TypeHint(annotation=annotation, is_pydantic=False)
        if is_pydantic(annotation):
            handler_io.output_type.is_pydantic=True
            if isinstance(handler_io.output_serde, DefaultSerde):
                handler_io.output_serde = PydanticJsonSerde(annotation)

# pylint: disable=R0902
@dataclass
class Handler(Generic[I, O]):
    """
    Represents a handler for a service.

    Attributes:
        service_tag: The service tag for the handler.
        handler_io: The input/output configuration for the handler.
        kind: The kind of handler (exclusive, shared, workflow).
        name: The name of the handler.
        fn: The handler function.
        arity: The number of parameters in the handler function.
        description: Documentation for this handler definition.
        metadata: Custom metadata for this handler definition.
        inactivity_timeout: Inactivity timeout duration.
        abort_timeout: Abort timeout duration.
        journal_retention: Journal retention duration.
        idempotency_retention: Idempotency retention duration.
        workflow_retention: Workflow completion retention duration.
        enable_lazy_state: If true, lazy state is enabled.
        ingress_private: If true, the handler cannot be invoked from the HTTP nor Kafka ingress.
    """
    service_tag: ServiceTag
    handler_io: HandlerIO[I, O]
    kind: Optional[Literal["exclusive", "shared", "workflow"]]
    name: str
    fn: Callable[[Any, I], Awaitable[O]] | Callable[[Any], Awaitable[O]]
    arity: int
    description: Optional[str] = None
    metadata: Optional[Dict[str, str]] = None
    inactivity_timeout: Optional[timedelta] = None
    abort_timeout: Optional[timedelta] = None
    journal_retention: Optional[timedelta] = None
    idempotency_retention: Optional[timedelta] = None
    workflow_retention: Optional[timedelta] = None
    enable_lazy_state: Optional[bool] = None
    ingress_private: Optional[bool] = None


# disable too many arguments warning
# pylint: disable=R0913
# pylint: disable=R0914
def make_handler(service_tag: ServiceTag,
                 handler_io: HandlerIO[I, O],
                 name: str | None,
                 kind: Optional[Literal["exclusive", "shared", "workflow"]],
                 wrapped: Any,
                 signature: Signature,
                 description: Optional[str] = None,
                 metadata: Optional[Dict[str, str]] = None,
                 inactivity_timeout: Optional[timedelta] = None,
                 abort_timeout: Optional[timedelta] = None,
                 journal_retention: Optional[timedelta] = None,
                 idempotency_retention: Optional[timedelta] = None,
                 workflow_retention: Optional[timedelta] = None,
                 enable_lazy_state: Optional[bool] = None,
                 ingress_private: Optional[bool] = None) -> Handler[I, O]:
    """
    Factory function to create a handler.

    Args:
        service_tag: The service tag for the handler.
        handler_io: The input/output configuration for the handler.
        name: The name of the handler.
        kind: The kind of handler (exclusive, shared, workflow).
        wrapped: The wrapped function.
        signature: The signature of the function.
        description: Documentation for this handler definition.
        metadata: Custom metadata for this handler definition.
        inactivity_timeout: Inactivity timeout duration.
        abort_timeout: Abort timeout duration.
        journal_retention: Journal retention duration.
        idempotency_retention: Idempotency retention duration.
        workflow_retention: Workflow completion retention duration.
        enable_lazy_state: If true, lazy state is enabled.
        ingress_private: If true, the handler cannot be invoked from the HTTP nor Kafka ingress.
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
    update_handler_io_with_type_hints(handler_io, signature) # mutates handler_io

    handler = Handler[I, O](service_tag=service_tag,
                            handler_io=handler_io,
                            kind=kind,
                            name=handler_name,
                            fn=wrapped,
                            arity=arity,
                            description=description,
                            metadata=metadata,
                            inactivity_timeout=inactivity_timeout,
                            abort_timeout=abort_timeout,
                            journal_retention=journal_retention,
                            idempotency_retention=idempotency_retention,
                            workflow_retention=workflow_retention,
                            enable_lazy_state=enable_lazy_state,
                            ingress_private=ingress_private)

    vars(wrapped)[RESTATE_UNIQUE_HANDLER_SYMBOL] = handler
    return handler

def handler_from_callable(wrapper: HandlerType[I, O]) -> Handler[I, O]:
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
            in_arg = handler.handler_io.input_serde.deserialize(in_buffer)
        except Exception as e:
            raise TerminalError(message=f"Unable to parse an input argument. {e}") from e
        out_arg = await handler.fn(ctx, in_arg) # type: ignore [call-arg, arg-type]
    else:
        out_arg = await handler.fn(ctx) # type: ignore [call-arg]
    out_buffer = handler.handler_io.output_serde.serialize(out_arg)
    return bytes(out_buffer)
