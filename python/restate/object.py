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
This module defines the Service class for representing a restate service.
"""

from functools import wraps
import inspect
import typing

from restate.serde import Serde, JsonSerde
from .handler import HandlerIO, ServiceTag, make_handler

I = typing.TypeVar('I')
O = typing.TypeVar('O')


# disable too many arguments warning
# pylint: disable=R0913

# disable line too long warning
# pylint: disable=C0301

class VirtualObject:
    """
    Represents a restate virtual object.

    Args:
        name (str): The name of the object.
    """

    def __init__(self, name):
        self.service_tag = ServiceTag("object", name)
        self.handlers = {}

    @property
    def name(self):
        """
        Returns the name of the object.
        """
        return self.service_tag.name

    def handler(self,
                name: typing.Optional[str] = None,
                kind: typing.Optional[typing.Literal["exclusive", "shared"]] = "exclusive",
                accept: str = "application/json",
                content_type: str = "application/json",
                input_serde: Serde[I] = JsonSerde[I](), # type: ignore
                output_serde: Serde[O] = JsonSerde[O]()) -> typing.Callable: # type: ignore
        """
        Decorator for defining a handler function.

        Args:
            name: The name of the handler. 
            accept: The accept type of the request. Default "application/json".
            content_type: The content type of the request. Default "application/json".
            serializer: The serializer function to convert the response object to bytes. 
            deserializer: The deserializer function to convert the request bytes to an object.

        Returns:
            Callable: The decorated function.

        Raises:
            ValueError: If the handler name is not provided.

        Example:
            @service.handler()
            def my_handler_func(ctx, request):
                # handler logic
                pass
        """
        handler_io = HandlerIO[I,O](accept, content_type, input_serde, output_serde)
        def wrapper(fn):

            @wraps(fn)
            def wrapped(*args, **kwargs):
                return fn(*args, **kwargs)

            signature = inspect.signature(fn)
            handler = make_handler(self.service_tag, handler_io, name, kind, wrapped, signature)
            self.handlers[handler.name] = handler
            return wrapped

        return wrapper
