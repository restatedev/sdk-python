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
Class-based API for defining Restate services.

This module provides an alternative to the decorator-based API, allowing
services to be defined as classes with handler methods.

Example::

    from restate.cls import Service, VirtualObject, Workflow, handler, shared, main
    import restate

    class Greeter(Service):
        @handler
        async def greet(self, name: str) -> str:
            return f"Hello {name}!"

    class Counter(VirtualObject):
        @handler
        async def increment(self, value: int) -> int:
            n = await restate.get("counter", type_hint=int) or 0
            n += value
            restate.set("counter", n)
            return n

        @shared
        async def count(self) -> int:
            return await restate.get("counter", type_hint=int) or 0
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import timedelta
from functools import wraps
from typing import Any, AsyncContextManager, Callable, Dict, List, Literal, Optional, TypeVar

from restate.handler import HandlerIO, ServiceTag, make_handler
from restate.retry_policy import InvocationRetryPolicy
from restate.serde import DefaultSerde, Serde

# Imports for type annotations only — the actual classes are used
# as companion objects inside __init_subclass__.
from restate.service import Service as _OriginalService
from restate.object import VirtualObject as _OriginalVirtualObject
from restate.workflow import Workflow as _OriginalWorkflow

I = TypeVar("I")
O = TypeVar("O")
T = TypeVar("T")

# ── Handler marker decorators ──────────────────────────────────────────────

_HANDLER_MARKER = "__restate_handler_meta__"

_MISSING = object()


@dataclass
class _HandlerMeta:
    """Metadata attached to a method by @handler / @shared / @main."""

    kind: Literal["handler", "shared", "main"]
    name: Optional[str] = None
    accept: str = "application/json"
    content_type: str = "application/json"
    input_serde: Serde = field(default_factory=DefaultSerde)
    output_serde: Serde = field(default_factory=DefaultSerde)
    metadata: Optional[Dict[str, str]] = None
    inactivity_timeout: Optional[timedelta] = None
    abort_timeout: Optional[timedelta] = None
    journal_retention: Optional[timedelta] = None
    idempotency_retention: Optional[timedelta] = None
    workflow_retention: Optional[timedelta] = None
    enable_lazy_state: Optional[bool] = None
    ingress_private: Optional[bool] = None
    invocation_retry_policy: Optional[InvocationRetryPolicy] = None
    invocation_context_managers: Optional[List[Callable[[], AsyncContextManager[None]]]] = None


def handler(
    fn=None,
    *,
    name: Optional[str] = None,
    accept: str = "application/json",
    content_type: str = "application/json",
    input_serde: Serde[I] = DefaultSerde(),
    output_serde: Serde[O] = DefaultSerde(),
    metadata: Optional[Dict[str, str]] = None,
    inactivity_timeout: Optional[timedelta] = None,
    abort_timeout: Optional[timedelta] = None,
    journal_retention: Optional[timedelta] = None,
    idempotency_retention: Optional[timedelta] = None,
    enable_lazy_state: Optional[bool] = None,
    ingress_private: Optional[bool] = None,
    invocation_retry_policy: Optional[InvocationRetryPolicy] = None,
    invocation_context_managers: Optional[List[Callable[[], AsyncContextManager[None]]]] = None,
):
    """Mark a method as a Restate handler.

    For Service: a regular handler.
    For VirtualObject: an exclusive handler.
    For Workflow: a shared handler.

    Can be used as ``@handler`` or ``@handler(name="customName")``.
    Supports the same parameters as the decorator-based API.
    """

    def decorator(fn):
        setattr(
            fn,
            _HANDLER_MARKER,
            _HandlerMeta(
                kind="handler",
                name=name,
                accept=accept,
                content_type=content_type,
                input_serde=input_serde,
                output_serde=output_serde,
                metadata=metadata,
                inactivity_timeout=inactivity_timeout,
                abort_timeout=abort_timeout,
                journal_retention=journal_retention,
                idempotency_retention=idempotency_retention,
                enable_lazy_state=enable_lazy_state,
                ingress_private=ingress_private,
                invocation_retry_policy=invocation_retry_policy,
                invocation_context_managers=invocation_context_managers,
            ),
        )
        return fn

    if fn is not None:
        return decorator(fn)
    return decorator


def shared(
    fn=None,
    *,
    name: Optional[str] = None,
    accept: str = "application/json",
    content_type: str = "application/json",
    input_serde: Serde[I] = DefaultSerde(),
    output_serde: Serde[O] = DefaultSerde(),
    metadata: Optional[Dict[str, str]] = None,
    inactivity_timeout: Optional[timedelta] = None,
    abort_timeout: Optional[timedelta] = None,
    journal_retention: Optional[timedelta] = None,
    idempotency_retention: Optional[timedelta] = None,
    enable_lazy_state: Optional[bool] = None,
    ingress_private: Optional[bool] = None,
    invocation_retry_policy: Optional[InvocationRetryPolicy] = None,
    invocation_context_managers: Optional[List[Callable[[], AsyncContextManager[None]]]] = None,
):
    """Mark a method as a shared (read-only) handler on a VirtualObject or Workflow."""

    def decorator(fn):
        setattr(
            fn,
            _HANDLER_MARKER,
            _HandlerMeta(
                kind="shared",
                name=name,
                accept=accept,
                content_type=content_type,
                input_serde=input_serde,
                output_serde=output_serde,
                metadata=metadata,
                inactivity_timeout=inactivity_timeout,
                abort_timeout=abort_timeout,
                journal_retention=journal_retention,
                idempotency_retention=idempotency_retention,
                enable_lazy_state=enable_lazy_state,
                ingress_private=ingress_private,
                invocation_retry_policy=invocation_retry_policy,
                invocation_context_managers=invocation_context_managers,
            ),
        )
        return fn

    if fn is not None:
        return decorator(fn)
    return decorator


def main(
    fn=None,
    *,
    name: Optional[str] = None,
    accept: str = "application/json",
    content_type: str = "application/json",
    input_serde: Serde[I] = DefaultSerde(),
    output_serde: Serde[O] = DefaultSerde(),
    metadata: Optional[Dict[str, str]] = None,
    inactivity_timeout: Optional[timedelta] = None,
    abort_timeout: Optional[timedelta] = None,
    journal_retention: Optional[timedelta] = None,
    workflow_retention: Optional[timedelta] = None,
    enable_lazy_state: Optional[bool] = None,
    ingress_private: Optional[bool] = None,
    invocation_retry_policy: Optional[InvocationRetryPolicy] = None,
    invocation_context_managers: Optional[List[Callable[[], AsyncContextManager[None]]]] = None,
):
    """Mark a method as the workflow entry point."""

    def decorator(fn):
        setattr(
            fn,
            _HANDLER_MARKER,
            _HandlerMeta(
                kind="main",
                name=name,
                accept=accept,
                content_type=content_type,
                input_serde=input_serde,
                output_serde=output_serde,
                metadata=metadata,
                inactivity_timeout=inactivity_timeout,
                abort_timeout=abort_timeout,
                journal_retention=journal_retention,
                workflow_retention=workflow_retention,
                enable_lazy_state=enable_lazy_state,
                ingress_private=ingress_private,
                invocation_retry_policy=invocation_retry_policy,
                invocation_context_managers=invocation_context_managers,
            ),
        )
        return fn

    if fn is not None:
        return decorator(fn)
    return decorator


# ── Class processing ───────────────────────────────────────────────────────


def _resolve_handler_kind(
    service_kind: Literal["service", "object", "workflow"],
    marker_kind: Literal["handler", "shared", "main"],
) -> Optional[Literal["exclusive", "shared", "workflow"]]:
    """Map (service_type, marker) → Handler.kind value."""
    if service_kind == "service":
        return None
    if service_kind == "object":
        if marker_kind == "handler":
            return "exclusive"
        if marker_kind == "shared":
            return "shared"
        raise ValueError(f"VirtualObject does not support @{marker_kind}")
    if service_kind == "workflow":
        if marker_kind == "main":
            return "workflow"
        if marker_kind == "handler":
            return "shared"
        if marker_kind == "shared":
            return "shared"
        raise ValueError(f"Workflow does not support @{marker_kind}")
    raise ValueError(f"Unknown service kind: {service_kind}")


@dataclass
class _ServiceConfig:
    """Service-level configuration extracted from __init_subclass__ kwargs."""

    name: Optional[str] = None
    description: Optional[str] = None
    metadata: Optional[Dict[str, str]] = None
    inactivity_timeout: Optional[timedelta] = None
    abort_timeout: Optional[timedelta] = None
    journal_retention: Optional[timedelta] = None
    idempotency_retention: Optional[timedelta] = None
    enable_lazy_state: Optional[bool] = None
    ingress_private: Optional[bool] = None
    invocation_retry_policy: Optional[InvocationRetryPolicy] = None
    invocation_context_managers: Optional[List[Callable[[], AsyncContextManager[None]]]] = None


def _process_class(
    cls: type,
    service_kind: Literal["service", "object", "workflow"],
    config: _ServiceConfig,
) -> None:
    """Scan *cls* for marked methods and build the companion service object."""
    name = config.name or cls.__name__

    service_tag = ServiceTag(
        kind=service_kind,
        name=name,
        description=config.description,
        metadata=config.metadata,
    )
    handlers: Dict[str, Any] = {}

    for attr_name, attr_value in list(cls.__dict__.items()):
        meta: Optional[_HandlerMeta] = getattr(attr_value, _HANDLER_MARKER, None)
        if meta is None:
            continue

        method = attr_value  # unbound function
        handler_kind = _resolve_handler_kind(service_kind, meta.kind)
        handler_name = meta.name or method.__name__

        # Create a wrapper that instantiates the class and calls the method.
        # The wrapper has signature (ctx, *args) matching what invoke_handler expects.
        @wraps(method)
        async def wrapper(ctx, *args, _method=method, _cls=cls):
            instance = object.__new__(_cls)
            if args:
                return await _method(instance, *args)
            return await _method(instance)

        # Use the original method's signature for type/serde inspection
        sig = inspect.signature(method, eval_str=True)
        handler_io: HandlerIO = HandlerIO(
            accept=meta.accept,
            content_type=meta.content_type,
            input_serde=meta.input_serde,
            output_serde=meta.output_serde,
        )

        # Combine service-level and handler-level context managers
        combined_context_managers = (
            (config.invocation_context_managers or []) + (meta.invocation_context_managers or [])
            if config.invocation_context_managers or meta.invocation_context_managers
            else None
        )

        h = make_handler(
            service_tag=service_tag,
            handler_io=handler_io,
            name=handler_name,
            kind=handler_kind,
            wrapped=wrapper,
            signature=sig,
            description=inspect.getdoc(method),
            metadata=meta.metadata,
            inactivity_timeout=meta.inactivity_timeout,
            abort_timeout=meta.abort_timeout,
            journal_retention=meta.journal_retention,
            idempotency_retention=meta.idempotency_retention,
            workflow_retention=meta.workflow_retention,
            enable_lazy_state=meta.enable_lazy_state,
            ingress_private=meta.ingress_private,
            invocation_retry_policy=meta.invocation_retry_policy,
            context_managers=combined_context_managers,
        )
        handlers[h.name] = h

    # Store handlers on the class for proxy access
    cls._restate_handlers = handlers  # type: ignore[attr-defined]

    # Build companion service object of the original type
    svc: _OriginalService | _OriginalVirtualObject | _OriginalWorkflow
    if service_kind == "service":
        svc = _OriginalService(
            name,
            description=config.description,
            metadata=config.metadata,
            inactivity_timeout=config.inactivity_timeout,
            abort_timeout=config.abort_timeout,
            journal_retention=config.journal_retention,
            idempotency_retention=config.idempotency_retention,
            ingress_private=config.ingress_private,
            invocation_retry_policy=config.invocation_retry_policy,
            invocation_context_managers=config.invocation_context_managers,
        )
    elif service_kind == "object":
        svc = _OriginalVirtualObject(
            name,
            description=config.description,
            metadata=config.metadata,
            inactivity_timeout=config.inactivity_timeout,
            abort_timeout=config.abort_timeout,
            journal_retention=config.journal_retention,
            idempotency_retention=config.idempotency_retention,
            enable_lazy_state=config.enable_lazy_state,
            ingress_private=config.ingress_private,
            invocation_retry_policy=config.invocation_retry_policy,
            invocation_context_managers=config.invocation_context_managers,
        )
    elif service_kind == "workflow":
        svc = _OriginalWorkflow(
            name,
            description=config.description,
            metadata=config.metadata,
            inactivity_timeout=config.inactivity_timeout,
            abort_timeout=config.abort_timeout,
            journal_retention=config.journal_retention,
            idempotency_retention=config.idempotency_retention,
            enable_lazy_state=config.enable_lazy_state,
            ingress_private=config.ingress_private,
            invocation_retry_policy=config.invocation_retry_policy,
            invocation_context_managers=config.invocation_context_managers,
        )
    else:
        raise ValueError(f"Unknown service kind: {service_kind}")

    svc.handlers = handlers
    cls._restate_service = svc  # type: ignore[attr-defined]


# ── Fluent RPC proxy classes ──────────────────────────────────────────────


class _ServiceCallProxy:
    """Proxy returned by Service.call() for type-safe RPC."""

    def __init__(self, cls: type) -> None:
        self._cls = cls

    def __getattr__(self, name: str):
        handlers = getattr(self._cls, "_restate_handlers", {})
        h = handlers.get(name)
        if h is None:
            raise AttributeError(f"No handler '{name}' on {self._cls.__name__}")
        from restate.server_context import _restate_context_var  # pylint: disable=C0415

        ctx = _restate_context_var.get()

        def invoke(arg=_MISSING):
            if arg is _MISSING:
                return ctx.service_call(h.fn, arg=None)
            return ctx.service_call(h.fn, arg=arg)

        return invoke


class _ServiceSendProxy:
    """Proxy returned by Service.send() for fire-and-forget."""

    def __init__(self, cls: type, delay: Optional[timedelta] = None) -> None:
        self._cls = cls
        self._delay = delay

    def __getattr__(self, name: str):
        handlers = getattr(self._cls, "_restate_handlers", {})
        h = handlers.get(name)
        if h is None:
            raise AttributeError(f"No handler '{name}' on {self._cls.__name__}")
        from restate.server_context import _restate_context_var  # pylint: disable=C0415

        ctx = _restate_context_var.get()

        def invoke(arg=_MISSING):
            if arg is _MISSING:
                return ctx.service_send(h.fn, arg=None, send_delay=self._delay)
            return ctx.service_send(h.fn, arg=arg, send_delay=self._delay)

        return invoke


class _ObjectCallProxy:
    """Proxy returned by VirtualObject.call(key) for type-safe RPC."""

    def __init__(self, cls: type, key: str) -> None:
        self._cls = cls
        self._key = key

    def __getattr__(self, name: str):
        handlers = getattr(self._cls, "_restate_handlers", {})
        h = handlers.get(name)
        if h is None:
            raise AttributeError(f"No handler '{name}' on {self._cls.__name__}")
        from restate.server_context import _restate_context_var  # pylint: disable=C0415

        ctx = _restate_context_var.get()

        def invoke(arg=_MISSING):
            if arg is _MISSING:
                return ctx.object_call(h.fn, key=self._key, arg=None)
            return ctx.object_call(h.fn, key=self._key, arg=arg)

        return invoke


class _ObjectSendProxy:
    """Proxy returned by VirtualObject.send(key) for fire-and-forget."""

    def __init__(self, cls: type, key: str, delay: Optional[timedelta] = None) -> None:
        self._cls = cls
        self._key = key
        self._delay = delay

    def __getattr__(self, name: str):
        handlers = getattr(self._cls, "_restate_handlers", {})
        h = handlers.get(name)
        if h is None:
            raise AttributeError(f"No handler '{name}' on {self._cls.__name__}")
        from restate.server_context import _restate_context_var  # pylint: disable=C0415

        ctx = _restate_context_var.get()

        def invoke(arg=_MISSING):
            if arg is _MISSING:
                return ctx.object_send(h.fn, key=self._key, arg=None, send_delay=self._delay)
            return ctx.object_send(h.fn, key=self._key, arg=arg, send_delay=self._delay)

        return invoke


class _WorkflowCallProxy:
    """Proxy returned by Workflow.call(key) for type-safe RPC."""

    def __init__(self, cls: type, key: str) -> None:
        self._cls = cls
        self._key = key

    def __getattr__(self, name: str):
        handlers = getattr(self._cls, "_restate_handlers", {})
        h = handlers.get(name)
        if h is None:
            raise AttributeError(f"No handler '{name}' on {self._cls.__name__}")
        from restate.server_context import _restate_context_var  # pylint: disable=C0415

        ctx = _restate_context_var.get()

        def invoke(arg=_MISSING):
            if arg is _MISSING:
                return ctx.workflow_call(h.fn, key=self._key, arg=None)
            return ctx.workflow_call(h.fn, key=self._key, arg=arg)

        return invoke


class _WorkflowSendProxy:
    """Proxy returned by Workflow.send(key) for fire-and-forget."""

    def __init__(self, cls: type, key: str, delay: Optional[timedelta] = None) -> None:
        self._cls = cls
        self._key = key
        self._delay = delay

    def __getattr__(self, name: str):
        handlers = getattr(self._cls, "_restate_handlers", {})
        h = handlers.get(name)
        if h is None:
            raise AttributeError(f"No handler '{name}' on {self._cls.__name__}")
        from restate.server_context import _restate_context_var  # pylint: disable=C0415

        ctx = _restate_context_var.get()

        def invoke(arg=_MISSING):
            if arg is _MISSING:
                return ctx.workflow_send(h.fn, key=self._key, arg=None, send_delay=self._delay)
            return ctx.workflow_send(h.fn, key=self._key, arg=arg, send_delay=self._delay)

        return invoke


# ── Base classes ───────────────────────────────────────────────────────────


class Service:
    """Base class for class-based Restate services.

    Supports the same service-level configuration as the decorator-based API::

        class Greeter(Service, name="MyGreeter", ingress_private=True):
            @handler
            async def greet(self, name: str) -> str:
                return f"Hello {name}!"

        app = restate.app([Greeter])
    """

    _restate_service: _OriginalService
    _restate_handlers: Dict[str, Any]

    def __init_subclass__(
        cls,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
        inactivity_timeout: Optional[timedelta] = None,
        abort_timeout: Optional[timedelta] = None,
        journal_retention: Optional[timedelta] = None,
        idempotency_retention: Optional[timedelta] = None,
        ingress_private: Optional[bool] = None,
        invocation_retry_policy: Optional[InvocationRetryPolicy] = None,
        invocation_context_managers: Optional[List[Callable[[], AsyncContextManager[None]]]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        _process_class(
            cls,
            "service",
            _ServiceConfig(
                name=name,
                description=description,
                metadata=metadata,
                inactivity_timeout=inactivity_timeout,
                abort_timeout=abort_timeout,
                journal_retention=journal_retention,
                idempotency_retention=idempotency_retention,
                ingress_private=ingress_private,
                invocation_retry_policy=invocation_retry_policy,
                invocation_context_managers=invocation_context_managers,
            ),
        )

    @classmethod
    def call(cls) -> "Service":  # type: ignore[return-type]
        """Return a proxy for making durable service calls.

        The proxy has the same method signatures as the class,
        giving full IDE autocomplete and type inference.
        """
        return _ServiceCallProxy(cls)  # type: ignore[return-value]

    @classmethod
    def send(cls, *, delay: Optional[timedelta] = None) -> "Service":  # type: ignore[return-type]
        """Return a proxy for fire-and-forget service sends."""
        return _ServiceSendProxy(cls, delay)  # type: ignore[return-value]


class VirtualObject:
    """Base class for class-based Restate virtual objects.

    Supports the same service-level configuration as the decorator-based API::

        class Counter(VirtualObject, name="MyCounter", enable_lazy_state=True):
            @handler
            async def increment(self, value: int) -> int: ...

        app = restate.app([Counter])
    """

    _restate_service: _OriginalVirtualObject
    _restate_handlers: Dict[str, Any]

    def __init_subclass__(
        cls,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
        inactivity_timeout: Optional[timedelta] = None,
        abort_timeout: Optional[timedelta] = None,
        journal_retention: Optional[timedelta] = None,
        idempotency_retention: Optional[timedelta] = None,
        enable_lazy_state: Optional[bool] = None,
        ingress_private: Optional[bool] = None,
        invocation_retry_policy: Optional[InvocationRetryPolicy] = None,
        invocation_context_managers: Optional[List[Callable[[], AsyncContextManager[None]]]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        _process_class(
            cls,
            "object",
            _ServiceConfig(
                name=name,
                description=description,
                metadata=metadata,
                inactivity_timeout=inactivity_timeout,
                abort_timeout=abort_timeout,
                journal_retention=journal_retention,
                idempotency_retention=idempotency_retention,
                enable_lazy_state=enable_lazy_state,
                ingress_private=ingress_private,
                invocation_retry_policy=invocation_retry_policy,
                invocation_context_managers=invocation_context_managers,
            ),
        )

    @classmethod
    def call(cls, key: str) -> "VirtualObject":  # type: ignore[return-type]
        """Return a proxy for making durable object calls."""
        return _ObjectCallProxy(cls, key)  # type: ignore[return-value]

    @classmethod
    def send(cls, key: str, *, delay: Optional[timedelta] = None) -> "VirtualObject":  # type: ignore[return-type]
        """Return a proxy for fire-and-forget object sends."""
        return _ObjectSendProxy(cls, key, delay)  # type: ignore[return-value]


class Workflow:
    """Base class for class-based Restate workflows.

    Supports the same service-level configuration as the decorator-based API::

        class Payment(Workflow, name="MyPayment"):
            @main
            async def pay(self, amount: int) -> dict: ...

        app = restate.app([Payment])
    """

    _restate_service: _OriginalWorkflow
    _restate_handlers: Dict[str, Any]

    def __init_subclass__(
        cls,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
        inactivity_timeout: Optional[timedelta] = None,
        abort_timeout: Optional[timedelta] = None,
        journal_retention: Optional[timedelta] = None,
        idempotency_retention: Optional[timedelta] = None,
        enable_lazy_state: Optional[bool] = None,
        ingress_private: Optional[bool] = None,
        invocation_retry_policy: Optional[InvocationRetryPolicy] = None,
        invocation_context_managers: Optional[List[Callable[[], AsyncContextManager[None]]]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        _process_class(
            cls,
            "workflow",
            _ServiceConfig(
                name=name,
                description=description,
                metadata=metadata,
                inactivity_timeout=inactivity_timeout,
                abort_timeout=abort_timeout,
                journal_retention=journal_retention,
                idempotency_retention=idempotency_retention,
                enable_lazy_state=enable_lazy_state,
                ingress_private=ingress_private,
                invocation_retry_policy=invocation_retry_policy,
                invocation_context_managers=invocation_context_managers,
            ),
        )

    @classmethod
    def call(cls, key: str) -> "Workflow":  # type: ignore[return-type]
        """Return a proxy for making durable workflow calls."""
        return _WorkflowCallProxy(cls, key)  # type: ignore[return-value]

    @classmethod
    def send(cls, key: str, *, delay: Optional[timedelta] = None) -> "Workflow":  # type: ignore[return-type]
        """Return a proxy for fire-and-forget workflow sends."""
        return _WorkflowSendProxy(cls, key, delay)  # type: ignore[return-value]


# ── Context accessor class ────────────────────────────────────────────────


class Context:
    """Static accessor for the current Restate invocation context.

    Use from within handler methods to access Restate functionality
    without an explicit ``ctx`` parameter::

        from restate.cls import Service, handler, Context

        class Greeter(Service):
            @handler
            async def greet(self, name: str) -> str:
                count = await Context.get("visits", type_hint=int) or 0
                Context.set("visits", count + 1)
                return f"Hello {name}!"
    """

    @staticmethod
    def _ctx() -> Any:
        from restate.server_context import _restate_context_var  # pylint: disable=C0415

        try:
            return _restate_context_var.get()
        except LookupError:
            raise RuntimeError(
                "Not inside a Restate handler. Context methods can only be called within a handler invocation."
            ) from None

    # ── State ──

    @staticmethod
    def get(name: str, serde: Serde = DefaultSerde(), type_hint: Optional[type] = None) -> Any:
        """Retrieve a state value by name."""
        return Context._ctx().get(name, serde=serde, type_hint=type_hint)

    @staticmethod
    def set(name: str, value: Any, serde: Serde = DefaultSerde()) -> None:
        """Set a state value by name."""
        Context._ctx().set(name, value, serde=serde)

    @staticmethod
    def clear(name: str) -> None:
        """Clear a state value by name."""
        Context._ctx().clear(name)

    @staticmethod
    def clear_all() -> None:
        """Clear all state values."""
        Context._ctx().clear_all()

    @staticmethod
    def state_keys() -> Any:
        """Return the list of state keys."""
        return Context._ctx().state_keys()

    # ── Identity & request ──

    @staticmethod
    def key() -> str:
        """Return the key of the current virtual object or workflow."""
        return Context._ctx().key()

    @staticmethod
    def request() -> Any:
        """Return the current request object."""
        return Context._ctx().request()

    @staticmethod
    def random() -> Any:
        """Return a deterministically-seeded Random instance."""
        return Context._ctx().random()

    @staticmethod
    def uuid() -> Any:
        """Return a deterministic UUID, stable across retries."""
        return Context._ctx().uuid()

    @staticmethod
    def time() -> Any:
        """Return a durable timestamp, stable across retries."""
        return Context._ctx().time()

    # ── Durable execution ──

    @staticmethod
    def run(name: str, action: Any, serde: Serde = DefaultSerde(), **kwargs: Any) -> Any:
        """Run a durable side effect (deprecated — use run_typed)."""
        return Context._ctx().run(name, action, serde=serde, **kwargs)

    @staticmethod
    def run_typed(name: str, action: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a durable side effect with typed arguments."""
        return Context._ctx().run_typed(name, action, *args, **kwargs)

    @staticmethod
    def sleep(delta: timedelta, name: Optional[str] = None) -> Any:
        """Suspend the current invocation for the given duration."""
        return Context._ctx().sleep(delta, name=name)

    # ── Service communication ──

    @staticmethod
    def service_call(tpe: Any, arg: Any, **kwargs: Any) -> Any:
        """Call a service handler."""
        return Context._ctx().service_call(tpe, arg=arg, **kwargs)

    @staticmethod
    def service_send(tpe: Any, arg: Any, **kwargs: Any) -> Any:
        """Send a message to a service handler (fire-and-forget)."""
        return Context._ctx().service_send(tpe, arg=arg, **kwargs)

    @staticmethod
    def object_call(tpe: Any, key: str, arg: Any, **kwargs: Any) -> Any:
        """Call a virtual object handler."""
        return Context._ctx().object_call(tpe, key=key, arg=arg, **kwargs)

    @staticmethod
    def object_send(tpe: Any, key: str, arg: Any, **kwargs: Any) -> Any:
        """Send a message to a virtual object handler (fire-and-forget)."""
        return Context._ctx().object_send(tpe, key=key, arg=arg, **kwargs)

    @staticmethod
    def workflow_call(tpe: Any, key: str, arg: Any, **kwargs: Any) -> Any:
        """Call a workflow handler."""
        return Context._ctx().workflow_call(tpe, key=key, arg=arg, **kwargs)

    @staticmethod
    def workflow_send(tpe: Any, key: str, arg: Any, **kwargs: Any) -> Any:
        """Send a message to a workflow handler (fire-and-forget)."""
        return Context._ctx().workflow_send(tpe, key=key, arg=arg, **kwargs)

    @staticmethod
    def generic_call(service: str, handler: str, arg: bytes, key: Optional[str] = None, **kwargs: Any) -> Any:
        """Call a generic service/handler with raw bytes."""
        return Context._ctx().generic_call(service, handler, arg, key=key, **kwargs)

    @staticmethod
    def generic_send(service: str, handler: str, arg: bytes, key: Optional[str] = None, **kwargs: Any) -> Any:
        """Send a message to a generic service/handler with raw bytes."""
        return Context._ctx().generic_send(service, handler, arg, key=key, **kwargs)

    # ── Awakeables ──

    @staticmethod
    def awakeable(serde: Serde = DefaultSerde(), type_hint: Optional[type] = None) -> Any:
        """Create an awakeable and return (id, future)."""
        return Context._ctx().awakeable(serde=serde, type_hint=type_hint)

    @staticmethod
    def resolve_awakeable(name: str, value: Any, serde: Serde = DefaultSerde()) -> None:
        """Resolve an awakeable by id."""
        Context._ctx().resolve_awakeable(name, value, serde=serde)

    @staticmethod
    def reject_awakeable(name: str, failure_message: str, failure_code: int = 500) -> None:
        """Reject an awakeable by id."""
        Context._ctx().reject_awakeable(name, failure_message, failure_code=failure_code)

    # ── Promises (Workflow only) ──

    @staticmethod
    def promise(name: str, serde: Serde = DefaultSerde(), type_hint: Optional[type] = None) -> Any:
        """Return a durable promise (workflow handlers only)."""
        return Context._ctx().promise(name, serde=serde, type_hint=type_hint)

    # ── Invocation management ──

    @staticmethod
    def cancel_invocation(invocation_id: str) -> None:
        """Cancel an invocation by id."""
        Context._ctx().cancel_invocation(invocation_id)

    @staticmethod
    def attach_invocation(invocation_id: str, serde: Serde = DefaultSerde(), type_hint: Optional[type] = None) -> Any:
        """Attach to an invocation by id."""
        return Context._ctx().attach_invocation(invocation_id, serde=serde, type_hint=type_hint)
