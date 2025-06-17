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
# pylint: disable=R0801
"""
This module defines the Service class for representing a restate service.
"""

from functools import wraps
import inspect
import typing
from datetime import timedelta

from restate.serde import Serde, DefaultSerde
from restate.handler import Handler, HandlerIO, ServiceTag, make_handler

I = typing.TypeVar('I')
O = typing.TypeVar('O')


# disable too many arguments warning
# pylint: disable=R0913

# disable line too long warning
# pylint: disable=C0301

# pylint: disable=R0902
class VirtualObject:
    """
    Represents a restate virtual object.

    Args:
        name (str): The name of the object.
        description (str, optional): Documentation as shown in the UI, Admin REST API, and the generated OpenAPI documentation of this service.
        metadata (Dict[str, str], optional): Service metadata, as propagated in the Admin REST API.
        inactivity_timeout (timedelta, optional): This timer guards against stalled invocations. Once it expires, Restate triggers a graceful
            termination by asking the invocation to suspend (which preserves intermediate progress).
            The abort_timeout is used to abort the invocation, in case it doesn't react to the request to suspend.
            This overrides the default inactivity timeout configured in the restate-server for all invocations to this service.
            NOTE: You can set this field only if you register this service against restate-server >= 1.4,
            otherwise the service discovery will fail.
        abort_timeout (timedelta, optional): This timer guards against stalled service/handler invocations that are supposed to terminate. The
            abort timeout is started after the inactivity_timeout has expired and the service/handler invocation has been asked to gracefully terminate.
            Once the timer expires, it will abort the service/handler invocation.
            This timer potentially *interrupts* user code. If the user code needs longer to gracefully terminate, then this value needs to be set accordingly.
            This overrides the default abort timeout configured in the restate-server for all invocations to this service.
            NOTE: You can set this field only if you register this service against restate-server >= 1.4,
            otherwise the service discovery will fail.
        journal_retention (timedelta, optional): The journal retention. When set, this applies to all requests to all handlers of this service.
            In case the request has an idempotency key, the idempotency_retention caps the journal retention time.
            NOTE: You can set this field only if you register this service against restate-server >= 1.4,
            otherwise the service discovery will fail.
        idempotency_retention (timedelta, optional): The retention duration of idempotent requests to this service.
            NOTE: You can set this field only if you register this service against restate-server >= 1.4,
            otherwise the service discovery will fail.
        enable_lazy_state (bool, optional): When set to True, lazy state will be enabled for all invocations to this service.
            NOTE: You can set this field only if you register this service against restate-server >= 1.4,
            otherwise the service discovery will fail.
        ingress_private (bool, optional): When set to True this service, with all its handlers, cannot be invoked from the restate-server
            HTTP and Kafka ingress, but only from other services.
            NOTE: You can set this field only if you register this service against restate-server >= 1.4,
            otherwise the service discovery will fail.
    """

    handlers: typing.Dict[str, Handler[typing.Any, typing.Any]]

    def __init__(self, name,
                 description: typing.Optional[str] = None,
                 metadata: typing.Optional[typing.Dict[str, str]] = None,
                 inactivity_timeout: typing.Optional[timedelta] = None,
                 abort_timeout: typing.Optional[timedelta] = None,
                 journal_retention: typing.Optional[timedelta] = None,
                 idempotency_retention: typing.Optional[timedelta] = None,
                 enable_lazy_state: typing.Optional[bool] = None,
                 ingress_private: typing.Optional[bool] = None):
        self.service_tag = ServiceTag("object", name, description, metadata)
        self.handlers = {}
        self.inactivity_timeout = inactivity_timeout
        self.abort_timeout = abort_timeout
        self.journal_retention = journal_retention
        self.idempotency_retention = idempotency_retention
        self.enable_lazy_state = enable_lazy_state
        self.ingress_private = ingress_private

    @property
    def name(self):
        """
        Returns the name of the object.
        """
        return self.service_tag.name

    # pylint: disable=R0914
    def handler(self,
                name: typing.Optional[str] = None,
                kind: typing.Optional[typing.Literal["exclusive", "shared"]] = "exclusive",
                accept: str = "application/json",
                content_type: str = "application/json",
                input_serde: Serde[I] = DefaultSerde(),
                output_serde: Serde[O] = DefaultSerde(),
                metadata: typing.Optional[typing.Dict[str, str]] = None,
                inactivity_timeout: typing.Optional[timedelta] = None,
                abort_timeout: typing.Optional[timedelta] = None,
                journal_retention: typing.Optional[timedelta] = None,
                idempotency_retention: typing.Optional[timedelta] = None,
                enable_lazy_state: typing.Optional[bool] = None,
                ingress_private: typing.Optional[bool] = None) -> typing.Callable:
        """
        Decorator for defining a handler function.

        Args:
            name: The name of the handler. 
            kind: The kind of handler (exclusive, shared). Default "exclusive".
            accept: Set the acceptable content type when ingesting HTTP requests. Wildcards can be used, e.g.
                `application/*` or `*/*`. Default "application/json".
            content_type: The content type of the request. Default "application/json".
            input_serde: The serializer/deserializer for the input parameter.
            output_serde: The serializer/deserializer for the output result.
            metadata: Handler metadata, as propagated in the Admin REST API.
            inactivity_timeout: This timer guards against stalled invocations. Once it expires, Restate triggers a graceful
                termination by asking the invocation to suspend (which preserves intermediate progress).
                The abort_timeout is used to abort the invocation, in case it doesn't react to the request to suspend.
                This overrides the inactivity timeout set for the service and the default set in restate-server.
                NOTE: You can set this field only if you register this service against restate-server >= 1.4,
                otherwise the service discovery will fail.
            abort_timeout: This timer guards against stalled invocations that are supposed to terminate. The abort timeout
                is started after the inactivity_timeout has expired and the invocation has been asked to gracefully terminate.
                Once the timer expires, it will abort the invocation.
                This timer potentially *interrupts* user code. If the user code needs longer to gracefully terminate, then this value needs to be set accordingly.
                This overrides the abort timeout set for the service and the default set in restate-server.
                NOTE: You can set this field only if you register this service against restate-server >= 1.4,
                otherwise the service discovery will fail.
            journal_retention: The journal retention for invocations to this handler.
                In case the request has an idempotency key, the idempotency_retention caps the journal retention time.
                NOTE: You can set this field only if you register this service against restate-server >= 1.4,
                otherwise the service discovery will fail.
            idempotency_retention: The retention duration of idempotent requests to this service.
                NOTE: You can set this field only if you register this service against restate-server >= 1.4,
                otherwise the service discovery will fail.
            enable_lazy_state: When set to True, lazy state will be enabled for all invocations to this handler.
                NOTE: You can set this field only if you register this service against restate-server >= 1.4,
                otherwise the service discovery will fail.
            ingress_private: When set to True this handler cannot be invoked from the restate-server HTTP and Kafka ingress,
                but only from other services.
                NOTE: You can set this field only if you register this service against restate-server >= 1.4,
                otherwise the service discovery will fail.

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

            signature = inspect.signature(fn, eval_str=True)
            handler = make_handler(self.service_tag, handler_io, name, kind, wrapped, signature, inspect.getdoc(fn), metadata,
                                  inactivity_timeout, abort_timeout, journal_retention, idempotency_retention,
                                  None, enable_lazy_state, ingress_private)
            self.handlers[handler.name] = handler
            return wrapped

        return wrapper
