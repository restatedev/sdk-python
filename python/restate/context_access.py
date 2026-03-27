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
Module-level context accessor functions for the class-based API.

These functions delegate to the current invocation context via contextvars,
allowing handlers to access Restate functionality without an explicit ctx parameter.
"""

from datetime import timedelta
from random import Random
from typing import Any, Awaitable, Callable, Coroutine, Dict, List, Optional, Tuple, TypeVar, Union
from uuid import UUID

from restate.context import (
    DurablePromise,
    HandlerType,
    RestateDurableCallFuture,
    RestateDurableFuture,
    RestateDurableSleepFuture,
    Request,
    RunAction,
    RunOptions,
    SendHandle,
)
from restate.serde import DefaultSerde, Serde

T = TypeVar("T")
I = TypeVar("I")
O = TypeVar("O")


def _ctx() -> Any:
    """Get the current restate context, raising if not inside a handler.

    Returns Any because the actual runtime type is ServerInvocationContext
    (which implements ObjectContext, WorkflowContext, etc.) but we want all
    methods accessible without narrowing — runtime raises if mismatched.
    """
    # Import here to avoid circular imports
    from restate.server_context import _restate_context_var  # pylint: disable=C0415

    try:
        return _restate_context_var.get()
    except LookupError:
        raise RuntimeError(
            "Not inside a Restate handler. "
            "Module-level restate functions can only be called within a handler invocation."
        ) from None


def current_context():
    """Get the current Restate context.

    Returns the context object for the current handler invocation.
    Raises RuntimeError if called outside a handler.
    """
    return _ctx()


# ── State operations ──


def get(name: str, serde: Serde[T] = DefaultSerde(), type_hint: Optional[type] = None) -> Awaitable[Optional[T]]:
    """Retrieve a state value by name."""
    return _ctx().get(name, serde=serde, type_hint=type_hint)


def set(name: str, value: T, serde: Serde[T] = DefaultSerde()) -> None:
    """Set a state value by name."""
    _ctx().set(name, value, serde=serde)


def clear(name: str) -> None:
    """Clear a state value by name."""
    _ctx().clear(name)


def clear_all() -> None:
    """Clear all state values."""
    _ctx().clear_all()


def state_keys() -> Awaitable[List[str]]:
    """Return the list of state keys."""
    return _ctx().state_keys()


# ── Identity & request ──


def key() -> str:
    """Return the key of the current virtual object or workflow."""
    return _ctx().key()


def request() -> Request:
    """Return the current request object."""
    return _ctx().request()


def random() -> Random:
    """Return a deterministically-seeded Random instance."""
    return _ctx().random()


def uuid() -> UUID:
    """Return a deterministic UUID, stable across retries."""
    return _ctx().uuid()


def time() -> RestateDurableFuture[float]:
    """Return a durable timestamp, stable across retries."""
    return _ctx().time()


# ── Durable execution ──


def run(
    name: str,
    action: RunAction[T],
    serde: Serde[T] = DefaultSerde(),
    max_attempts: Optional[int] = None,
    max_retry_duration: Optional[timedelta] = None,
    type_hint: Optional[type] = None,
    args: Optional[tuple] = None,
) -> RestateDurableFuture[T]:
    """Run a durable side effect (deprecated — use run_typed instead)."""
    return _ctx().run(
        name,
        action,
        serde=serde,
        max_attempts=max_attempts,
        max_retry_duration=max_retry_duration,
        type_hint=type_hint,
        args=args,
    )


def run_typed(
    name: str,
    action: Union[Callable[..., Coroutine[Any, Any, T]], Callable[..., T]],
    options: RunOptions[T] = RunOptions(),
    /,
    *args: Any,
    **kwargs: Any,
) -> RestateDurableFuture[T]:
    """Run a durable side effect with typed arguments."""
    return _ctx().run_typed(name, action, options, *args, **kwargs)


def sleep(delta: timedelta, name: Optional[str] = None) -> RestateDurableSleepFuture:
    """Suspend the current invocation for the given duration."""
    return _ctx().sleep(delta, name=name)


# ── Service communication ──


def service_call(
    tpe: HandlerType[I, O],
    arg: I,
    idempotency_key: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> RestateDurableCallFuture[O]:
    """Call a service handler."""
    return _ctx().service_call(tpe, arg=arg, idempotency_key=idempotency_key, headers=headers)


def service_send(
    tpe: HandlerType[I, O],
    arg: I,
    send_delay: Optional[timedelta] = None,
    idempotency_key: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> SendHandle:
    """Send a message to a service handler (fire-and-forget)."""
    return _ctx().service_send(tpe, arg=arg, send_delay=send_delay, idempotency_key=idempotency_key, headers=headers)


def object_call(
    tpe: HandlerType[I, O],
    key: str,
    arg: I,
    idempotency_key: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> RestateDurableCallFuture[O]:
    """Call a virtual object handler."""
    return _ctx().object_call(tpe, key=key, arg=arg, idempotency_key=idempotency_key, headers=headers)


def object_send(
    tpe: HandlerType[I, O],
    key: str,
    arg: I,
    send_delay: Optional[timedelta] = None,
    idempotency_key: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> SendHandle:
    """Send a message to a virtual object handler (fire-and-forget)."""
    return _ctx().object_send(
        tpe, key=key, arg=arg, send_delay=send_delay, idempotency_key=idempotency_key, headers=headers
    )


def workflow_call(
    tpe: HandlerType[I, O],
    key: str,
    arg: I,
    idempotency_key: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> RestateDurableCallFuture[O]:
    """Call a workflow handler."""
    return _ctx().workflow_call(tpe, key=key, arg=arg, idempotency_key=idempotency_key, headers=headers)


def workflow_send(
    tpe: HandlerType[I, O],
    key: str,
    arg: I,
    send_delay: Optional[timedelta] = None,
    idempotency_key: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> SendHandle:
    """Send a message to a workflow handler (fire-and-forget)."""
    return _ctx().workflow_send(
        tpe, key=key, arg=arg, send_delay=send_delay, idempotency_key=idempotency_key, headers=headers
    )


def generic_call(
    service: str,
    handler: str,
    arg: bytes,
    key: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> RestateDurableCallFuture[bytes]:
    """Call a generic service/handler with raw bytes."""
    return _ctx().generic_call(service, handler, arg, key=key, idempotency_key=idempotency_key, headers=headers)


def generic_send(
    service: str,
    handler: str,
    arg: bytes,
    key: Optional[str] = None,
    send_delay: Optional[timedelta] = None,
    idempotency_key: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> SendHandle:
    """Send a message to a generic service/handler with raw bytes."""
    return _ctx().generic_send(
        service, handler, arg, key=key, send_delay=send_delay, idempotency_key=idempotency_key, headers=headers
    )


# ── Awakeables ──


def awakeable(
    serde: Serde[T] = DefaultSerde(), type_hint: Optional[type] = None
) -> Tuple[str, RestateDurableFuture[T]]:
    """Create an awakeable and return (id, future)."""
    return _ctx().awakeable(serde=serde, type_hint=type_hint)


def resolve_awakeable(name: str, value: I, serde: Serde[I] = DefaultSerde()) -> None:
    """Resolve an awakeable by id."""
    _ctx().resolve_awakeable(name, value, serde=serde)


def reject_awakeable(name: str, failure_message: str, failure_code: int = 500) -> None:
    """Reject an awakeable by id."""
    _ctx().reject_awakeable(name, failure_message, failure_code=failure_code)


# ── Promises (Workflow only) ──


def promise(name: str, serde: Serde[T] = DefaultSerde(), type_hint: Optional[type] = None) -> DurablePromise[T]:
    """Return a durable promise (workflow handlers only)."""
    return _ctx().promise(name, serde=serde, type_hint=type_hint)


# ── Invocation management ──


def cancel_invocation(invocation_id: str):
    """Cancel an invocation by id."""
    _ctx().cancel_invocation(invocation_id)


def attach_invocation(
    invocation_id: str, serde: Serde[T] = DefaultSerde(), type_hint: Optional[type] = None
) -> RestateDurableFuture[T]:
    """Attach to an invocation by id."""
    return _ctx().attach_invocation(invocation_id, serde=serde, type_hint=type_hint)
