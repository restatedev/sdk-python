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

from restate.retry_policy import InvocationRetryPolicy
from restate.serde import DefaultSerde, Serde
from restate.handler import Handler, HandlerIO, ServiceTag, check_state_preload_options, make_handler

I = typing.TypeVar("I")
O = typing.TypeVar("O")
T = typing.TypeVar("T")


# disable too many arguments warning
# pylint: disable=R0913

# disable line too long warning
# pylint: disable=C0301

# disable similar lines warning
# pylint: disable=R0801


# pylint: disable=R0902
class Workflow:
    """
    Represents a restate workflow.

    Args:
        name (str): The name of the workflow.
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
        enable_lazy_state (bool, optional): Deprecated, use `state_preload` instead.
        state_preload (bool | List[str], optional): Configures which state keys are preloaded ("eager state")
            at the start of each invocation to this workflow. Can be overridden per handler.
            - `True`: preload all state (this is the default behavior).
            - `False`: preload nothing (lazy state).
            - `["key1", "key2"]`: preload only the listed keys eagerly.
            NOTE: The list form is supported only by restate-server >= 1.8, otherwise the service discovery will fail.
        ingress_private (bool, optional): When set to True this service, with all its handlers, cannot be invoked from the restate-server
            HTTP and Kafka ingress, but only from other services.
            NOTE: You can set this field only if you register this service against restate-server >= 1.4,
            otherwise the service discovery will fail.
        invocation_retry_policy (InvocationRetryPolicy, optional): Retry policy applied for all invocations to this workflow.
        invocation_context_managers (List[Callable[[], AsyncContextManager[None]]], optional): List of context manager factories that will be applied to each invocation of this workflow.
        explicit_recovery (bool, optional): (Preview) Explicit recovery for all handlers of this workflow:
            see the handler option of the same name. NOTE: in preview, it might change without notice.
        explicit_cancellation (bool, optional): (Preview) Explicit cancellation for all handlers of this workflow:
            see the handler option of the same name. NOTE: in preview, it might change without notice.
    """

    handlers: typing.Dict[str, Handler[typing.Any, typing.Any]]

    def __restate_services__(self) -> typing.Tuple["Workflow", ...]:
        """What ``restate.app`` serves for this (see ``restate.Servable``): itself."""
        return (self,)

    def __init__(
        self,
        name: str,
        description: typing.Optional[str] = None,
        metadata: typing.Optional[typing.Dict[str, str]] = None,
        inactivity_timeout: typing.Optional[timedelta] = None,
        abort_timeout: typing.Optional[timedelta] = None,
        journal_retention: typing.Optional[timedelta] = None,
        idempotency_retention: typing.Optional[timedelta] = None,
        enable_lazy_state: typing.Optional[bool] = None,
        state_preload: typing.Optional[typing.Union[bool, typing.List[str]]] = None,
        ingress_private: typing.Optional[bool] = None,
        invocation_retry_policy: typing.Optional[InvocationRetryPolicy] = None,
        invocation_context_managers: typing.Optional[
            typing.List[typing.Callable[[], typing.AsyncContextManager[None]]]
        ] = None,
        explicit_recovery: bool = False,
        explicit_cancellation: bool = False,
    ):
        self.service_tag = ServiceTag("workflow", name, description, metadata)
        self.handlers = {}
        self.inactivity_timeout = inactivity_timeout
        self.abort_timeout = abort_timeout
        self.journal_retention = journal_retention
        self.idempotency_retention = idempotency_retention
        check_state_preload_options(state_preload, enable_lazy_state)
        self.enable_lazy_state = enable_lazy_state
        self.state_preload = state_preload
        self.ingress_private = ingress_private
        self.invocation_retry_policy = invocation_retry_policy
        self.context_managers = invocation_context_managers
        self.explicit_recovery = explicit_recovery
        self.explicit_cancellation = explicit_cancellation

    @property
    def name(self):
        """
        Returns the name of the object.
        """
        return self.service_tag.name

    def main(
        self,
        name: typing.Optional[str] = None,
        accept: str = "application/json",
        content_type: str = "application/json",
        input_serde: Serde[I] = DefaultSerde[I](),  # type: ignore
        output_serde: Serde[O] = DefaultSerde[O](),  # type: ignore
        metadata: typing.Optional[typing.Dict[str, str]] = None,
        inactivity_timeout: typing.Optional[timedelta] = None,
        abort_timeout: typing.Optional[timedelta] = None,
        journal_retention: typing.Optional[timedelta] = None,
        workflow_retention: typing.Optional[timedelta] = None,
        enable_lazy_state: typing.Optional[bool] = None,
        state_preload: typing.Optional[typing.Union[bool, typing.List[str]]] = None,
        ingress_private: typing.Optional[bool] = None,
        invocation_retry_policy: typing.Optional[InvocationRetryPolicy] = None,
        invocation_context_managers: typing.Optional[
            typing.List[typing.Callable[[], typing.AsyncContextManager[None]]]
        ] = None,
        explicit_recovery: bool = False,
        explicit_cancellation: bool = False,
    ) -> typing.Callable[[T], T]:
        """
        Mark this handler as a workflow entry point.

        Args:
            name: The name of the handler.
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
            workflow_retention: The retention duration for this workflow handler.
                NOTE: You can set this field only if you register this service against restate-server >= 1.4,
                otherwise the service discovery will fail.
            enable_lazy_state: Deprecated, use `state_preload` instead.
            state_preload: Configures which state keys are preloaded ("eager state") at the start of each
                invocation to this handler. Overrides the value set on the service.
                - `True`: preload all state (this is the default behavior).
                - `False`: preload nothing (lazy state).
                - `["key1", "key2"]`: preload only the listed keys eagerly.
                NOTE: The list form is supported only by restate-server >= 1.8, otherwise the service discovery will fail.
            ingress_private: When set to True this handler cannot be invoked from the restate-server HTTP and Kafka ingress,
                but only from other services.
                NOTE: You can set this field only if you register this service against restate-server >= 1.4,
                otherwise the service discovery will fail.
            invocation_retry_policy (InvocationRetryPolicy, optional): Retry policy applied for all invocations to this workflow.
            explicit_recovery: (Preview) If true, a retried or resumed invocation does not replay its journal:
                the handler runs again from the start, executing every context operation anew. So each
                attempt's calls and sends are new requests (give them an idempotency key that is the same on
                every attempt, such as one made from ``ctx.request().id``), ``ctx.run`` runs its action again
                on every attempt, and awakeables are not supported.
                NOTE: in preview, it might change without notice.
            explicit_cancellation: (Preview) If true, Restate cancelling an invocation neither fails the handler's
                awaits nor cancels its calls: the handler awaits `restate._preview.cancellation(ctx)` and decides
                what to stop. NOTE: in preview, it might change without notice.
        """
        return self._add_handler(
            name,
            kind="workflow",
            accept=accept,
            content_type=content_type,
            input_serde=input_serde,
            output_serde=output_serde,
            metadata=metadata,
            inactivity_timeout=inactivity_timeout,
            abort_timeout=abort_timeout,
            journal_retention=journal_retention,
            idempotency_retention=None,
            workflow_retention=workflow_retention,
            enable_lazy_state=enable_lazy_state,
            state_preload=state_preload,
            ingress_private=ingress_private,
            invocation_retry_policy=invocation_retry_policy,
            invocation_context_managers=invocation_context_managers,
            explicit_recovery=explicit_recovery,
            explicit_cancellation=explicit_cancellation,
        )

    def handler(
        self,
        name: typing.Optional[str] = None,
        accept: str = "application/json",
        content_type: str = "application/json",
        input_serde: Serde[I] = DefaultSerde[I](),  # type: ignore
        output_serde: Serde[O] = DefaultSerde[O](),  # type: ignore
        metadata: typing.Optional[typing.Dict[str, str]] = None,
        inactivity_timeout: typing.Optional[timedelta] = None,
        abort_timeout: typing.Optional[timedelta] = None,
        journal_retention: typing.Optional[timedelta] = None,
        idempotency_retention: typing.Optional[timedelta] = None,
        enable_lazy_state: typing.Optional[bool] = None,
        state_preload: typing.Optional[typing.Union[bool, typing.List[str]]] = None,
        ingress_private: typing.Optional[bool] = None,
        invocation_retry_policy: typing.Optional[InvocationRetryPolicy] = None,
        invocation_context_managers: typing.Optional[
            typing.List[typing.Callable[[], typing.AsyncContextManager[None]]]
        ] = None,
        explicit_recovery: bool = False,
        explicit_cancellation: bool = False,
    ) -> typing.Callable[[T], T]:
        """
        Decorator for defining a handler function.

        Args:
            name: The name of the handler.
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
            enable_lazy_state: Deprecated, use `state_preload` instead.
            state_preload: Configures which state keys are preloaded ("eager state") at the start of each
                invocation to this handler. Overrides the value set on the service.
                - `True`: preload all state (this is the default behavior).
                - `False`: preload nothing (lazy state).
                - `["key1", "key2"]`: preload only the listed keys eagerly.
                NOTE: The list form is supported only by restate-server >= 1.8, otherwise the service discovery will fail.
            ingress_private: When set to True this handler cannot be invoked from the restate-server HTTP and Kafka ingress,
                but only from other services.
                NOTE: You can set this field only if you register this service against restate-server >= 1.4,
                otherwise the service discovery will fail.
            invocation_retry_policy (InvocationRetryPolicy, optional): Retry policy applied for all invocations to this handler.
            invocation_context_managers (List[Callable[[], AsyncContextManager[None]]], optional): List of context manager factories that will be applied to each invocation of this handler.
            explicit_recovery: (Preview) If true, a retried or resumed invocation does not replay its journal:
                the handler runs again from the start, executing every context operation anew. So each
                attempt's calls and sends are new requests (give them an idempotency key that is the same on
                every attempt, such as one made from ``ctx.request().id``), ``ctx.run`` runs its action again
                on every attempt, and awakeables are not supported.
                NOTE: in preview, it might change without notice.
            explicit_cancellation: (Preview) If true, Restate cancelling an invocation neither fails the handler's
                awaits nor cancels its calls: the handler awaits `restate._preview.cancellation(ctx)` and decides
                what to stop. NOTE: in preview, it might change without notice.
        """
        return self._add_handler(
            name,
            "shared",
            accept,
            content_type,
            input_serde,
            output_serde,
            metadata,
            inactivity_timeout,
            abort_timeout,
            journal_retention,
            idempotency_retention,
            None,
            enable_lazy_state,
            state_preload,
            ingress_private,
            invocation_retry_policy,
            invocation_context_managers,
            explicit_recovery=explicit_recovery,
            explicit_cancellation=explicit_cancellation,
        )

    # pylint: disable=R0914
    def _add_handler(
        self,
        name: typing.Optional[str] = None,
        kind: typing.Literal["workflow", "shared", "exclusive"] = "shared",
        accept: str = "application/json",
        content_type: str = "application/json",
        input_serde: Serde[I] = DefaultSerde[I](),  # type: ignore
        output_serde: Serde[O] = DefaultSerde[O](),  # type: ignore
        metadata: typing.Optional[typing.Dict[str, str]] = None,
        inactivity_timeout: typing.Optional[timedelta] = None,
        abort_timeout: typing.Optional[timedelta] = None,
        journal_retention: typing.Optional[timedelta] = None,
        idempotency_retention: typing.Optional[timedelta] = None,
        workflow_retention: typing.Optional[timedelta] = None,
        enable_lazy_state: typing.Optional[bool] = None,
        state_preload: typing.Optional[typing.Union[bool, typing.List[str]]] = None,
        ingress_private: typing.Optional[bool] = None,
        invocation_retry_policy: typing.Optional["InvocationRetryPolicy"] = None,
        invocation_context_managers: typing.Optional[
            typing.List[typing.Callable[[], typing.AsyncContextManager[None]]]
        ] = None,
        explicit_recovery: bool = False,
        explicit_cancellation: bool = False,
    ) -> typing.Callable[[T], T]:
        """
        Decorator for defining a handler function.

        Args:
            name: The name of the handler.
            kind: The kind of handler (workflow, shared, exclusive). Default "shared".
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
            workflow_retention: The retention duration for this workflow handler.
                NOTE: You can set this field only if you register this service against restate-server >= 1.4,
                otherwise the service discovery will fail.
            enable_lazy_state: Deprecated, use `state_preload` instead.
            state_preload: Configures which state keys are preloaded ("eager state") at the start of each
                invocation to this handler. Overrides the value set on the service.
                - `True`: preload all state (this is the default behavior).
                - `False`: preload nothing (lazy state).
                - `["key1", "key2"]`: preload only the listed keys eagerly.
                NOTE: The list form is supported only by restate-server >= 1.8, otherwise the service discovery will fail.
            ingress_private: When set to True this handler cannot be invoked from the restate-server HTTP and Kafka ingress,
                but only from other services.
                NOTE: You can set this field only if you register this service against restate-server >= 1.4,
                otherwise the service discovery will fail.
            invocation_retry_policy (InvocationRetryPolicy, optional): Retry policy applied for all invocations to this handler.

        Returns:
            Callable: The decorated function.

        Raises:
            ValueError: If the handler name is not provided, or a handler with the same name is already registered.

        Example:
            @service.handler()
            def my_handler_func(ctx, request):
                # handler logic
                pass
        """
        handler_io = HandlerIO[I, O](accept, content_type, input_serde, output_serde)

        def wrapper(fn):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                return fn(*args, **kwargs)

            signature = inspect.signature(fn, eval_str=True)
            description = inspect.getdoc(fn)
            combined_context_managers = (
                (self.context_managers or []) + (invocation_context_managers or [])
                if self.context_managers or invocation_context_managers
                else None
            )
            handler = make_handler(
                service_tag=self.service_tag,
                handler_io=handler_io,
                name=name,
                kind=kind,
                wrapped=wrapped,
                signature=signature,
                description=description,
                metadata=metadata,
                inactivity_timeout=inactivity_timeout,
                abort_timeout=abort_timeout,
                journal_retention=journal_retention,
                idempotency_retention=idempotency_retention,
                workflow_retention=workflow_retention,
                enable_lazy_state=enable_lazy_state,
                state_preload=state_preload,
                ingress_private=ingress_private,
                invocation_retry_policy=invocation_retry_policy,
                context_managers=combined_context_managers,
                explicit_recovery=explicit_recovery,
                explicit_cancellation=explicit_cancellation,
            )
            if handler.name in self.handlers:
                raise ValueError(f"Handler {handler.name} already exists in {self.name}")
            self.handlers[handler.name] = handler
            return wrapped

        return typing.cast(typing.Callable[[T], T], wrapper)
