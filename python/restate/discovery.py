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
"""
Holds the discovery API objects as defined by the restate protocol.
Note that the classes defined here do not use snake case, because they 
are intended to be serialized to JSON, and their cases must remain in the 
case that the restate server understands.
"""

# disable to few parameters
# pylint: disable=R0903
# pylint: disable=C0301
# pylint: disable=C0115
# pylint: disable=C0103
# pylint: disable=W0622
# pylint: disable=R0913,
# pylint: disable=R0917,

import json
import typing
from enum import Enum
from typing import Dict, Optional, Any, List, get_args, get_origin


from restate.endpoint import Endpoint as RestateEndpoint
from restate.handler import TypeHint

class ProtocolMode(Enum):
    BIDI_STREAM = "BIDI_STREAM"
    REQUEST_RESPONSE = "REQUEST_RESPONSE"

class ServiceType(Enum):
    VIRTUAL_OBJECT = "VIRTUAL_OBJECT"
    SERVICE = "SERVICE"
    WORKFLOW = "WORKFLOW"

class ServiceHandlerType(Enum):
    WORKFLOW = "WORKFLOW"
    EXCLUSIVE = "EXCLUSIVE"
    SHARED = "SHARED"

class InputPayload:
    def __init__(self, required: bool, contentType: str, jsonSchema: Optional[Any] = None):
        self.required = required
        self.contentType = contentType
        self.jsonSchema = jsonSchema

class OutputPayload:
    def __init__(self, setContentTypeIfEmpty: bool, contentType: Optional[str] = None, jsonSchema: Optional[Any] = None):
        self.contentType = contentType
        self.setContentTypeIfEmpty = setContentTypeIfEmpty
        self.jsonSchema = jsonSchema

class Handler:
    # pylint: disable=R0902
    def __init__(self, name: str, ty: Optional[ServiceHandlerType] = None, input: Optional[InputPayload | Dict[str, str]] = None, output: Optional[OutputPayload] = None, description: Optional[str] = None, metadata: Optional[Dict[str, str]] = None, inactivityTimeout: Optional[int] = None, abortTimeout: Optional[int] = None, journalRetention: Optional[int] = None, idempotencyRetention: Optional[int] = None, workflowCompletionRetention: Optional[int] = None, enableLazyState: Optional[bool] = None, ingressPrivate: Optional[bool] = None):
        self.name = name
        self.ty = ty
        self.input = input
        self.output = output
        self.documentation = description
        self.metadata = metadata
        self.inactivityTimeout = inactivityTimeout
        self.abortTimeout = abortTimeout
        self.journalRetention = journalRetention
        self.idempotencyRetention = idempotencyRetention
        self.workflowCompletionRetention = workflowCompletionRetention
        self.enableLazyState = enableLazyState
        self.ingressPrivate = ingressPrivate

class Service:
    # pylint: disable=R0902
    def __init__(self, name: str, ty: ServiceType, handlers: List[Handler], description: Optional[str] = None, metadata: Optional[Dict[str, str]] = None, inactivityTimeout: Optional[int] = None, abortTimeout: Optional[int] = None, journalRetention: Optional[int] = None, idempotencyRetention: Optional[int] = None, enableLazyState: Optional[bool] = None, ingressPrivate: Optional[bool] = None):
        self.name = name
        self.ty = ty
        self.handlers = handlers
        self.documentation = description
        self.metadata = metadata
        self.inactivityTimeout = inactivityTimeout
        self.abortTimeout = abortTimeout
        self.journalRetention = journalRetention
        self.idempotencyRetention = idempotencyRetention
        self.enableLazyState = enableLazyState
        self.ingressPrivate = ingressPrivate

class Endpoint:
    def __init__(self, protocolMode: ProtocolMode, minProtocolVersion: int, maxProtocolVersion: int, services: List[Service]):
        self.protocolMode = protocolMode
        self.minProtocolVersion = minProtocolVersion
        self.maxProtocolVersion = maxProtocolVersion
        self.services = services

PROTOCOL_MODES = {
        "bidi" : ProtocolMode.BIDI_STREAM,
        "request_response" : ProtocolMode.REQUEST_RESPONSE}

SERVICE_TYPES = {
            "service": ServiceType.SERVICE,
            "object": ServiceType.VIRTUAL_OBJECT,
            "workflow": ServiceType.WORKFLOW}

HANDLER_TYPES  = {
            'exclusive': ServiceHandlerType.EXCLUSIVE,
            'shared': ServiceHandlerType.SHARED,
            'workflow': ServiceHandlerType.WORKFLOW}

class PythonClassEncoder(json.JSONEncoder):
    """
    Serialize Python objects as JSON
    """
    def default(self, o):
        if isinstance(o, Enum):
            return o.value
        return {key: value for key, value in o.__dict__.items() if value is not None}


# pylint: disable=R0911
def type_hint_to_json_schema(type_hint: Any) -> Any:
    """
    Convert a Python type hint to a JSON schema.

    """
    origin = get_origin(type_hint) or type_hint
    args = get_args(type_hint)
    if origin is str:
        return {"type": "string"}
    if origin is int:
        return {"type": "integer"}
    if origin is float:
        return {"type": "number"}
    if origin is bool:
        return {"type": "boolean"}
    if origin is list:
        items = type_hint_to_json_schema(args[0] if args else Any)
        return {"type": "array", "items": items}
    if origin is dict:
        return {
            "type": "object"
        }
    if origin is None:
        return {"type": "null"}
    # Default to all valid schema
    return True

def json_schema_from_type_hint(type_hint: Optional[TypeHint[Any]]) -> Any:
    """
    Convert a type hint to a JSON schema.
    """
    if not type_hint:
        return None
    if not type_hint.annotation:
        return None
    if type_hint.is_pydantic:
        return type_hint.annotation.model_json_schema(mode='serialization')
    return type_hint_to_json_schema(type_hint.annotation)


# pylint: disable=R0912
def compute_discovery_json(endpoint: RestateEndpoint,
                           version: int,
                           discovered_as: typing.Literal["bidi", "request_response"]) -> str:
    """
    return restate's discovery object as JSON
    """

    ep = compute_discovery(endpoint, discovered_as)

    # Validate that new discovery fields aren't used with older protocol versions
    if version <= 2:
        # Check for new discovery fields in version 3 that shouldn't be used in version 2 or lower
        for service in ep.services:
            if service.inactivityTimeout is not None:
                raise ValueError("inactivityTimeout is only supported in discovery protocol version 3")
            if service.abortTimeout is not None:
                raise ValueError("abortTimeout is only supported in discovery protocol version 3")
            if service.idempotencyRetention is not None:
                raise ValueError("idempotencyRetention is only supported in discovery protocol version 3")
            if service.journalRetention is not None:
                raise ValueError("journalRetention is only supported in discovery protocol version 3")
            if service.enableLazyState is not None:
                raise ValueError("enableLazyState is only supported in discovery protocol version 3")
            if service.ingressPrivate is not None:
                raise ValueError("ingressPrivate is only supported in discovery protocol version 3")

            for handler in service.handlers:
                if handler.inactivityTimeout is not None:
                    raise ValueError("inactivityTimeout is only supported in discovery protocol version 3")
                if handler.abortTimeout is not None:
                    raise ValueError("abortTimeout is only supported in discovery protocol version 3")
                if handler.idempotencyRetention is not None:
                    raise ValueError("idempotencyRetention is only supported in discovery protocol version 3")
                if handler.journalRetention is not None:
                    raise ValueError("journalRetention is only supported in discovery protocol version 3")
                if handler.workflowCompletionRetention is not None:
                    raise ValueError("workflowCompletionRetention is only supported in discovery protocol version 3")
                if handler.enableLazyState is not None:
                    raise ValueError("enableLazyState is only supported in discovery protocol version 3")
                if handler.ingressPrivate is not None:
                    raise ValueError("ingressPrivate is only supported in discovery protocol version 3")

    json_str = json.dumps(ep, cls=PythonClassEncoder, allow_nan=False)
    return json_str


def compute_discovery(endpoint: RestateEndpoint, discovered_as : typing.Literal["bidi", "request_response"]) -> Endpoint:
    """
    return restate's discovery object for an endpoint
    """
    services: typing.List[Service] = []

    for service in endpoint.services.values():
        service_type = SERVICE_TYPES[service.service_tag.kind]
        service_handlers = []
        for handler in service.handlers.values():
            # type
            if handler.kind:
                ty = HANDLER_TYPES[handler.kind]
            else:
                ty = None
            # input
            inp: Optional[InputPayload | Dict[str, str]] = None
            if handler.handler_io.input_type and handler.handler_io.input_type.is_void:
                inp = {}
            else:
                inp = InputPayload(required=False,
                                   contentType=handler.handler_io.accept,
                                   jsonSchema=json_schema_from_type_hint(handler.handler_io.input_type))
            # output
            if handler.handler_io.output_type and handler.handler_io.output_type.is_void:
                out = OutputPayload(setContentTypeIfEmpty=False)
            else:
                out = OutputPayload(setContentTypeIfEmpty=False,
                                    contentType=handler.handler_io.content_type,
                                    jsonSchema=json_schema_from_type_hint(handler.handler_io.output_type))
            # add the handler
            service_handlers.append(Handler(name=handler.name,
                                            ty=ty,
                                            input=inp,
                                            output=out,
                                            description=handler.description,
                                            metadata=handler.metadata,
                                            inactivityTimeout=int(handler.inactivity_timeout.total_seconds() * 1000) if handler.inactivity_timeout else None,
                                            abortTimeout=int(handler.abort_timeout.total_seconds() * 1000) if handler.abort_timeout else None,
                                            journalRetention=int(handler.journal_retention.total_seconds() * 1000) if handler.journal_retention else None,
                                            idempotencyRetention=int(handler.idempotency_retention.total_seconds() * 1000) if handler.idempotency_retention else None,
                                            workflowCompletionRetention=int(handler.workflow_retention.total_seconds() * 1000) if handler.workflow_retention else None,
                                            enableLazyState=handler.enable_lazy_state,
                                            ingressPrivate=handler.ingress_private))
        # add the service
        description = service.service_tag.description
        metadata = service.service_tag.metadata
        services.append(Service(name=service.name,
                               ty=service_type,
                               handlers=service_handlers,
                               description=description,
                               metadata=metadata,
                               inactivityTimeout=int(service.inactivity_timeout.total_seconds() * 1000) if service.inactivity_timeout else None,
                               abortTimeout=int(service.abort_timeout.total_seconds() * 1000) if service.abort_timeout else None,
                               journalRetention=int(service.journal_retention.total_seconds() * 1000) if service.journal_retention else None,
                               idempotencyRetention=int(service.idempotency_retention.total_seconds() * 1000) if service.idempotency_retention else None,
                               enableLazyState=service.enable_lazy_state if hasattr(service, 'enable_lazy_state') else None,
                               ingressPrivate=service.ingress_private))

    if endpoint.protocol:
        protocol_mode = PROTOCOL_MODES[endpoint.protocol]
    else:
        protocol_mode = PROTOCOL_MODES[discovered_as]
    return Endpoint(protocolMode=protocol_mode,
                    minProtocolVersion=5,
                    maxProtocolVersion=5,
                    services=services)
