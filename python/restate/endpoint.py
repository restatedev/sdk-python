
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
This module defines the Endpoint class, which serves as a container for all the services and objects
"""

import typing

from restate.service import Service
from restate.object import VirtualObject
from restate.workflow import Workflow


# disable too few methods in a class
# pylint: disable=R0903


class Endpoint:
    """
    Endpoint service that contains all the services and objects
    """

    services: typing.Dict[str, typing.Union[Service, VirtualObject, Workflow]]
    protocol: typing.Optional[typing.Literal["bidi", "request_response"]]
    identity_keys: typing.List[str]

    def __init__(self):
        """
        Create a new restate endpoint that serves as a container for all the services and objects
        """
        self.services = {}
        # we will let the user to override it later perhaps, but for now let us
        # auto deduce it on discovery.
        # None means that the user did not explicitly set it.
        self.protocol = None

        self.identity_keys = []

    def bind(self, *services: typing.Union[Service, VirtualObject, Workflow]):
        """
        Bind a service to the endpoint

        Args:
            service: The service or virtual object to bind to the endpoint

        Raises:
            ValueError: If a service with the same name already exists in the endpoint

        Returns:
            The updated Endpoint instance
        """
        for service in services:
            if service.name in self.services:
                raise ValueError(f"Service {service.name} already exists")
            if isinstance(service, (Service, VirtualObject, Workflow)):
                self.services[service.name] = service
            else:
                raise ValueError(f"Invalid service type {service}")
        return self

    def streaming_protocol(self):
        """Use bidirectional streaming protocol. Use with servers that support HTTP2"""
        self.protocol = "bidi"
        return self

    def request_response_protocol(self):
        """Use request response style protocol for communication with restate."""
        self.protocol = "request_response"

    def identity_key(self, identity_key: str):
        """Add an identity key to this endpoint."""
        self.identity_keys.append(identity_key)

    def app(self):
        """
        Returns the ASGI application for this endpoint.

        This method is responsible for creating and returning the ASGI application
        that will handle incoming requests for this endpoint.

        Returns:
            The ASGI application for this endpoint.
       """
        # we need to import it here to avoid circular dependencies
        # pylint: disable=C0415
        # pylint: disable=R0401
        from restate.server import asgi_app
        return asgi_app(self)

def app(
    services: typing.Iterable[typing.Union[Service, VirtualObject, Workflow]],
    protocol: typing.Optional[typing.Literal["bidi", "request_response"]] = None,
    identity_keys: typing.Optional[typing.List[str]] = None):
    """A restate ASGI application that hosts the given services."""
    endpoint = Endpoint()
    if protocol == "bidi":
        endpoint.streaming_protocol()
    elif protocol == "request_response":
        endpoint.request_response_protocol()
    for service in services:
        endpoint.bind(service)
    if identity_keys:
        for key in identity_keys:
            endpoint.identity_key(key)
    return endpoint.app()
