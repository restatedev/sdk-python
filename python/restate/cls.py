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

This module lets you define Restate services as plain Python classes.
Under the hood, each class is transformed into the same primitives used
by the decorator-based API (``restate.Service``, ``restate.VirtualObject``,
``restate.Workflow``).

Transformation overview
-----------------------

Given user code like this::

    class Greeter(Service):
        def __init__(self, prefix: str):
            self.prefix = prefix

        @handler
        async def greet(self, name: str) -> str:
            return f"{self.prefix} {name}!"

    app = restate.app([Greeter("Hello")])

The following happens at **class definition time** (``__init_subclass__``):

1. ``_process_class`` scans ``Greeter.__dict__`` for methods marked with
   ``@handler``, ``@shared``, or ``@main``.

2. For each marked method it creates a **placeholder wrapper** with the
   signature ``(ctx, *args)`` that Restate's ``invoke_handler`` expects.
   This placeholder raises ``RuntimeError`` if called before binding.

3. The original method's signature is preserved separately for **type
   deduction** — ``inspect.signature(method)`` is passed to
   ``make_handler`` so that Pydantic/msgspec serde and JSON schemas
   are derived from the real ``(self, name: str) -> str`` annotations,
   not from the ``(*args)`` wrapper.

4. A **companion service object** (a plain ``restate.Service``,
   ``restate.VirtualObject``, or ``restate.Workflow``) is created and
   stored on the class as ``Greeter.__restate_service__``. This companion
   holds the handler dict and all service-level configuration.

Then at **bind time** (``restate.app([...])`` → ``Endpoint.bind``):

5. If a **class** is passed (``restate.app([Greeter])``), it is
   instantiated via ``Greeter()``. If the constructor requires arguments,
   a ``TypeError`` tells the user to pass an instance instead.

6. If an **instance** is passed (``restate.app([Greeter("Hello")])``),
   it is used directly.

7. ``_bind_instance(instance)`` is called, which replaces each handler's
   placeholder wrapper with a real one that **closes over the instance**.
   The companion ``__restate_service__`` is then registered with the
   endpoint just like any decorator-based service.

At **invocation time**, Restate calls the wrapper which dispatches
to the bound method via its closure — no class-level state involved::

    wrapper(ctx, "Alice")
      → _method = Greeter.greet        # captured in closure
      → _inst = Greeter("Hello") obj   # captured in closure
      → Greeter.greet(_inst, "Alice")
      → "Hello Alice!"

Example::

    from restate.cls import Service, VirtualObject, Workflow, handler, shared, main

    class Greeter(Service):
        @handler
        async def greet(self, name: str) -> str:
            return f"Hello {name}!"

    class Counter(VirtualObject):
        @handler
        async def increment(self, value: int) -> int:
            n = await Restate.get("counter", type_hint=int) or 0
            n += value
            Restate.set("counter", n)
            return n

        @shared
        async def count(self) -> int:
            return await Restate.get("counter", type_hint=int) or 0
"""

from __future__ import annotations

import copy
import inspect
import sys
from dataclasses import dataclass, field, replace
from datetime import timedelta
from functools import wraps
from typing import Any, AsyncContextManager, Callable, Dict, List, Literal, Optional, TypeVar

if sys.version_info >= (3, 11):
    from typing import Self
else:
    from typing_extensions import Self

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
    # Proxy lookup index: maps both handler names and Python method names.
    # Kept separate from svc.handlers which only has canonical handler names.
    handler_index: Dict[str, Any] = {}

    for attr_name, attr_value in list(cls.__dict__.items()):
        meta: Optional[_HandlerMeta] = getattr(attr_value, _HANDLER_MARKER, None)
        if meta is None:
            continue

        method = attr_value  # unbound function
        handler_kind = _resolve_handler_kind(service_kind, meta.kind)
        handler_name = meta.name or method.__name__

        # Placeholder wrapper — replaced by _bind_instance() at bind time
        # with one that closes over the actual instance.
        @wraps(method)
        async def wrapper(_ctx, *args, _handler_name=handler_name):
            raise RuntimeError(
                f"Handler {_handler_name} called before instance was bound. "
                f"Use restate.app([{cls.__name__}(...)]) to bind an instance."
            )

        # Use the original method's signature for type/serde inspection.
        # Note: arity is derived from this signature (including `self`),
        # which matches the (ctx, arg) calling convention of invoke_handler —
        # `self` occupies the same slot as `ctx` in the decorator-based API.
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
        handler_index[h.name] = h
        if method.__name__ != h.name:
            handler_index[method.__name__] = h

    # Store handler index on the class for proxy lookup (method name + handler name)
    cls.__restate_handlers__ = handler_index  # type: ignore[attr-defined]

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
    cls.__restate_service__ = svc  # type: ignore[attr-defined]


def _bind_instance(instance: Any) -> None:
    """Create real handler wrappers that close over *instance*.

    Called from ``Endpoint.bind()`` once the instance is known.
    Creates a **copy** of the companion service with new handler objects
    whose ``fn`` dispatches to the bound method on the instance.
    The copy is stored on the *instance* so that binding a second instance
    of the same class (e.g. to a different endpoint) does not clobber the first.
    """
    cls = type(instance)
    svc = cls.__restate_service__  # type: ignore[attr-defined]
    new_handlers: Dict[str, Any] = {}
    for handler_name, h in svc.handlers.items():
        method = cls.__dict__.get(handler_name)
        if method is None:
            # handler name might differ from method name
            for attr in cls.__dict__.values():
                meta = getattr(attr, _HANDLER_MARKER, None)
                if meta and meta.name == handler_name:
                    method = attr
                    break
        if method is None:
            new_handlers[handler_name] = h
            continue

        @wraps(method)
        async def wrapper(_ctx, *args, _method=method, _inst=instance):
            # _ctx is passed by invoke_handler but unused here;
            # context is accessed via Restate._ctx() (contextvars).
            if args:
                return await _method(_inst, *args)
            return await _method(_inst)

        new_handlers[handler_name] = replace(h, fn=wrapper)

    # Create a shallow copy of the companion service with the new handlers
    bound_svc = copy.copy(svc)
    bound_svc.handlers = new_handlers
    instance.__restate_service__ = bound_svc


# ── Fluent RPC proxy classes ──────────────────────────────────────────────


class _ServiceCallProxy:
    """Proxy returned by Service.call() for type-safe RPC."""

    def __init__(self, cls: type) -> None:
        self._cls = cls

    def __getattr__(self, name: str):
        handlers = getattr(self._cls, "__restate_handlers__", {})
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
        handlers = getattr(self._cls, "__restate_handlers__", {})
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
        handlers = getattr(self._cls, "__restate_handlers__", {})
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
        handlers = getattr(self._cls, "__restate_handlers__", {})
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
        handlers = getattr(self._cls, "__restate_handlers__", {})
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
        handlers = getattr(self._cls, "__restate_handlers__", {})
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

    __restate_service__: _OriginalService
    __restate_handlers__: Dict[str, Any]

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
    def call(cls) -> Self:  # type: ignore[return-type]
        """Return a proxy for making durable service calls.

        The proxy has the same method signatures as the class,
        giving full IDE autocomplete and type inference.
        """
        return _ServiceCallProxy(cls)  # type: ignore[return-value]

    @classmethod
    def send(cls, *, delay: Optional[timedelta] = None) -> Self:  # type: ignore[return-type]
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

    __restate_service__: _OriginalVirtualObject
    __restate_handlers__: Dict[str, Any]

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
    def call(cls, key: str) -> Self:  # type: ignore[return-type]
        """Return a proxy for making durable object calls."""
        return _ObjectCallProxy(cls, key)  # type: ignore[return-value]

    @classmethod
    def send(cls, key: str, *, delay: Optional[timedelta] = None) -> Self:  # type: ignore[return-type]
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

    __restate_service__: _OriginalWorkflow
    __restate_handlers__: Dict[str, Any]

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
    def call(cls, key: str) -> Self:  # type: ignore[return-type]
        """Return a proxy for making durable workflow calls."""
        return _WorkflowCallProxy(cls, key)  # type: ignore[return-value]

    @classmethod
    def send(cls, key: str, *, delay: Optional[timedelta] = None) -> Self:  # type: ignore[return-type]
        """Return a proxy for fire-and-forget workflow sends."""
        return _WorkflowSendProxy(cls, key, delay)  # type: ignore[return-value]


# ── Context accessor class ────────────────────────────────────────────────


class Restate:
    """Static accessor for the current Restate invocation context.

    Use from within handler methods to access Restate functionality
    without an explicit ``ctx`` parameter::

        from restate.cls import Service, handler, Restate

        class Greeter(Service):
            @handler
            async def greet(self, name: str) -> str:
                count = await Restate.get("visits", type_hint=int) or 0
                Restate.set("visits", count + 1)
                return f"Hello {name}!"
    """

    @staticmethod
    def _ctx() -> Any:
        from restate.context_access import current_context  # pylint: disable=C0415

        return current_context()

    # ── State ──

    @staticmethod
    def get(name: str, serde: Serde = DefaultSerde(), type_hint: Optional[type] = None) -> Any:
        """Retrieve a state value by name."""
        return Restate._ctx().get(name, serde=serde, type_hint=type_hint)

    @staticmethod
    def set(name: str, value: Any, serde: Serde = DefaultSerde()) -> None:
        """Set a state value by name."""
        Restate._ctx().set(name, value, serde=serde)

    @staticmethod
    def clear(name: str) -> None:
        """Clear a state value by name."""
        Restate._ctx().clear(name)

    @staticmethod
    def clear_all() -> None:
        """Clear all state values."""
        Restate._ctx().clear_all()

    @staticmethod
    def state_keys() -> Any:
        """Return the list of state keys."""
        return Restate._ctx().state_keys()

    # ── Identity & request ──

    @staticmethod
    def key() -> str:
        """Return the key of the current virtual object or workflow."""
        return Restate._ctx().key()

    @staticmethod
    def request() -> Any:
        """Return the current request object."""
        return Restate._ctx().request()

    @staticmethod
    def random() -> Any:
        """Return a deterministically-seeded Random instance."""
        return Restate._ctx().random()

    @staticmethod
    def uuid() -> Any:
        """Return a deterministic UUID, stable across retries."""
        return Restate._ctx().uuid()

    @staticmethod
    def time() -> Any:
        """Return a durable timestamp, stable across retries."""
        return Restate._ctx().time()

    # ── Durable execution ──

    @staticmethod
    def run(name: str, action: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a durable side effect with typed arguments."""
        return Restate._ctx().run_typed(name, action, *args, **kwargs)

    @staticmethod
    def sleep(delta: timedelta, name: Optional[str] = None) -> Any:
        """Suspend the current invocation for the given duration."""
        return Restate._ctx().sleep(delta, name=name)

    # ── Service communication ──

    @staticmethod
    def service_call(tpe: Any, arg: Any, **kwargs: Any) -> Any:
        """Call a service handler."""
        return Restate._ctx().service_call(tpe, arg=arg, **kwargs)

    @staticmethod
    def service_send(tpe: Any, arg: Any, **kwargs: Any) -> Any:
        """Send a message to a service handler (fire-and-forget)."""
        return Restate._ctx().service_send(tpe, arg=arg, **kwargs)

    @staticmethod
    def object_call(tpe: Any, key: str, arg: Any, **kwargs: Any) -> Any:
        """Call a virtual object handler."""
        return Restate._ctx().object_call(tpe, key=key, arg=arg, **kwargs)

    @staticmethod
    def object_send(tpe: Any, key: str, arg: Any, **kwargs: Any) -> Any:
        """Send a message to a virtual object handler (fire-and-forget)."""
        return Restate._ctx().object_send(tpe, key=key, arg=arg, **kwargs)

    @staticmethod
    def workflow_call(tpe: Any, key: str, arg: Any, **kwargs: Any) -> Any:
        """Call a workflow handler."""
        return Restate._ctx().workflow_call(tpe, key=key, arg=arg, **kwargs)

    @staticmethod
    def workflow_send(tpe: Any, key: str, arg: Any, **kwargs: Any) -> Any:
        """Send a message to a workflow handler (fire-and-forget)."""
        return Restate._ctx().workflow_send(tpe, key=key, arg=arg, **kwargs)

    @staticmethod
    def generic_call(service: str, handler: str, arg: bytes, key: Optional[str] = None, **kwargs: Any) -> Any:
        """Call a generic service/handler with raw bytes."""
        return Restate._ctx().generic_call(service, handler, arg, key=key, **kwargs)

    @staticmethod
    def generic_send(service: str, handler: str, arg: bytes, key: Optional[str] = None, **kwargs: Any) -> Any:
        """Send a message to a generic service/handler with raw bytes."""
        return Restate._ctx().generic_send(service, handler, arg, key=key, **kwargs)

    # ── Awakeables ──

    @staticmethod
    def awakeable(serde: Serde = DefaultSerde(), type_hint: Optional[type] = None) -> Any:
        """Create an awakeable and return (id, future)."""
        return Restate._ctx().awakeable(serde=serde, type_hint=type_hint)

    @staticmethod
    def resolve_awakeable(name: str, value: Any, serde: Serde = DefaultSerde()) -> None:
        """Resolve an awakeable by id."""
        Restate._ctx().resolve_awakeable(name, value, serde=serde)

    @staticmethod
    def reject_awakeable(name: str, failure_message: str, failure_code: int = 500) -> None:
        """Reject an awakeable by id."""
        Restate._ctx().reject_awakeable(name, failure_message, failure_code=failure_code)

    # ── Promises (Workflow only) ──

    @staticmethod
    def promise(name: str, serde: Serde = DefaultSerde(), type_hint: Optional[type] = None) -> Any:
        """Return a durable promise (workflow handlers only)."""
        return Restate._ctx().promise(name, serde=serde, type_hint=type_hint)

    # ── Invocation management ──

    @staticmethod
    def cancel_invocation(invocation_id: str) -> None:
        """Cancel an invocation by id."""
        Restate._ctx().cancel_invocation(invocation_id)

    @staticmethod
    def attach_invocation(invocation_id: str, serde: Serde = DefaultSerde(), type_hint: Optional[type] = None) -> Any:
        """Attach to an invocation by id."""
        return Restate._ctx().attach_invocation(invocation_id, serde=serde, type_hint=type_hint)
