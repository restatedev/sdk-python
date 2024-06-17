
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
This module defines the Endpoint class, which serves as a container for all the services and objects
"""

import typing

from restate.object import VirtualObject
from restate.service import Service

# disable too few methods in a class
# pylint: disable=R0903

class Endpoint:
    """
    Endpoint service that contains all the services and objects
    """
    def __init__(self):
        """
        Create a new restate endpoint that serves as a container for all the services and objects
        """
        self.services: typing.Dict[str, typing.Union[Service, VirtualObject]] = {}
        self.protocol: typing.Literal["bidi", "request_response"] = "bidi"

    def bind(self, service: typing.Union[Service, VirtualObject]):
        """
        Bind a service to the endpoint

        Args:
            service: The service or virtual object to bind to the endpoint

        Raises:
            ValueError: If a service with the same name already exists in the endpoint

        Returns:
            The updated Endpoint instance
        """
        if service.name in self.services:
            raise ValueError(f"Service {service.name} already exists")
        self.services[service.name] = service
        return self
