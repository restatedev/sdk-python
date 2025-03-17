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
    def __init__(self, contentType: str, setContentTypeIfEmpty: bool, jsonSchema: Optional[Any] = None):
        self.contentType = contentType
        self.setContentTypeIfEmpty = setContentTypeIfEmpty
        self.jsonSchema = jsonSchema

class Handler:
    def __init__(self, name: str, ty: Optional[ServiceHandlerType] = None, input: Optional[InputPayload] = None, output: Optional[OutputPayload] = None, description: Optional[str] = None, metadata: Optional[Dict[str, str]] = None):
        self.name = name
        self.ty = ty
        self.input = input
        self.output = output
        self.documentation = description
        self.metadata = metadata

class Service:
    def __init__(self, name: str, ty: ServiceType, handlers: List[Handler], description: Optional[str] = None, metadata: Optional[Dict[str, str]] = None):
        self.name = name
        self.ty = ty
        self.handlers = handlers
        self.documentation = description
        self.metadata = metadata

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
        return type_hint.annotation.model_json_schema(mode='serialization') # type: ignore
    return type_hint_to_json_schema(type_hint.annotation)



def compute_discovery_json(endpoint: RestateEndpoint,
                           version: int,
                           discovered_as: typing.Literal["bidi", "request_response"]) -> typing.Tuple[typing.Dict[str, str] ,str]:
    """
    return restate's discovery object as JSON 
    """
    if version != 1:
        raise ValueError(f"Unsupported protocol version {version}")

    ep = compute_discovery(endpoint, discovered_as)
    json_str = json.dumps(ep, cls=PythonClassEncoder, allow_nan=False)
    headers = {"content-type": "application/vnd.restate.endpointmanifest.v1+json"}
    return (headers, json_str)


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
            inp = InputPayload(required=False,
                               contentType=handler.handler_io.accept,
                               jsonSchema=json_schema_from_type_hint(handler.handler_io.input_type))
            # output
            out = OutputPayload(setContentTypeIfEmpty=False,
                                contentType=handler.handler_io.content_type,
                                jsonSchema=json_schema_from_type_hint(handler.handler_io.output_type))
            # add the handler
            service_handlers.append(Handler(name=handler.name,
                                            ty=ty,
                                            input=inp,
                                            output=out,
                                            description=handler.description,
                                            metadata=handler.metadata))
        # add the service
        description = service.service_tag.description
        metadata = service.service_tag.metadata
        services.append(Service(name=service.name, ty=service_type, handlers=service_handlers, description=description, metadata=metadata))

    if endpoint.protocol:
        protocol_mode = PROTOCOL_MODES[endpoint.protocol]
    else:
        protocol_mode = PROTOCOL_MODES[discovered_as]
    return Endpoint(protocolMode=protocol_mode,
                    minProtocolVersion=5,
                    maxProtocolVersion=5,
                    services=services)
