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
    RunOptions,
    SendHandle,
)
from restate.serde import DefaultSerde, Serde

T = TypeVar("T")
I = TypeVar("I")
O = TypeVar("O")


def current_context() -> Any:
    """Get the current Restate context.

    Returns the context object for the current handler invocation.
    Raises RuntimeError if called outside a handler.
    """
    from restate.server_context import _restate_context_var  # pylint: disable=C0415

    try:
        return _restate_context_var.get()
    except LookupError:
        raise RuntimeError(
            "Not inside a Restate handler. "
            "Module-level restate functions can only be called within a handler invocation."
        ) from None


# ── State operations ──


def get(name: str, serde: Serde[T] = DefaultSerde(), type_hint: Optional[type] = None) -> Awaitable[Optional[T]]:
    """Retrieve a state value by name."""
    return current_context().get(name, serde=serde, type_hint=type_hint)


def set(name: str, value: T, serde: Serde[T] = DefaultSerde()) -> None:
    """Set a state value by name."""
    current_context().set(name, value, serde=serde)


def clear(name: str) -> None:
    """Clear a state value by name."""
    current_context().clear(name)


def clear_all() -> None:
    """Clear all state values."""
    current_context().clear_all()


def state_keys() -> Awaitable[List[str]]:
    """Return the list of state keys."""
    return current_context().state_keys()


# ── Identity & request ──


def key() -> str:
    """Return the key of the current virtual object or workflow."""
    return current_context().key()


def request() -> Request:
    """Return the current request object."""
    return current_context().request()


def random() -> Random:
    """Return a deterministically-seeded Random instance."""
    return current_context().random()


def uuid() -> UUID:
    """Return a deterministic UUID, stable across retries."""
    return current_context().uuid()


def time() -> RestateDurableFuture[float]:
    """Return a durable timestamp, stable across retries."""
    return current_context().time()


# ── Durable execution ──


def run(
    name: str,
    action: Union[Callable[..., Coroutine[Any, Any, T]], Callable[..., T]],
    options: RunOptions[T] = RunOptions(),
    /,
    *args: Any,
    **kwargs: Any,
) -> RestateDurableFuture[T]:
    """Run a durable side effect with typed arguments."""
    return current_context().run_typed(name, action, options, *args, **kwargs)


def sleep(delta: timedelta, name: Optional[str] = None) -> RestateDurableSleepFuture:
    """Suspend the current invocation for the given duration."""
    return current_context().sleep(delta, name=name)


# ── Service communication ──


def service_call(
    tpe: HandlerType[I, O],
    arg: I,
    idempotency_key: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> RestateDurableCallFuture[O]:
    """Call a service handler."""
    return current_context().service_call(tpe, arg=arg, idempotency_key=idempotency_key, headers=headers)


def service_send(
    tpe: HandlerType[I, O],
    arg: I,
    send_delay: Optional[timedelta] = None,
    idempotency_key: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> SendHandle:
    """Send a message to a service handler (fire-and-forget)."""
    return current_context().service_send(
        tpe, arg=arg, send_delay=send_delay, idempotency_key=idempotency_key, headers=headers
    )


def object_call(
    tpe: HandlerType[I, O],
    key: str,
    arg: I,
    idempotency_key: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> RestateDurableCallFuture[O]:
    """Call a virtual object handler."""
    return current_context().object_call(tpe, key=key, arg=arg, idempotency_key=idempotency_key, headers=headers)


def object_send(
    tpe: HandlerType[I, O],
    key: str,
    arg: I,
    send_delay: Optional[timedelta] = None,
    idempotency_key: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> SendHandle:
    """Send a message to a virtual object handler (fire-and-forget)."""
    return current_context().object_send(
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
    return current_context().workflow_call(tpe, key=key, arg=arg, idempotency_key=idempotency_key, headers=headers)


def workflow_send(
    tpe: HandlerType[I, O],
    key: str,
    arg: I,
    send_delay: Optional[timedelta] = None,
    idempotency_key: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> SendHandle:
    """Send a message to a workflow handler (fire-and-forget)."""
    return current_context().workflow_send(
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
    return current_context().generic_call(
        service, handler, arg, key=key, idempotency_key=idempotency_key, headers=headers
    )


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
    return current_context().generic_send(
        service, handler, arg, key=key, send_delay=send_delay, idempotency_key=idempotency_key, headers=headers
    )


# ── Awakeables ──


def awakeable(
    serde: Serde[T] = DefaultSerde(), type_hint: Optional[type] = None
) -> Tuple[str, RestateDurableFuture[T]]:
    """Create an awakeable and return (id, future)."""
    return current_context().awakeable(serde=serde, type_hint=type_hint)


def resolve_awakeable(name: str, value: I, serde: Serde[I] = DefaultSerde()) -> None:
    """Resolve an awakeable by id."""
    current_context().resolve_awakeable(name, value, serde=serde)


def reject_awakeable(name: str, failure_message: str, failure_code: int = 500) -> None:
    """Reject an awakeable by id."""
    current_context().reject_awakeable(name, failure_message, failure_code=failure_code)


# ── Promises (Workflow only) ──


def promise(name: str, serde: Serde[T] = DefaultSerde(), type_hint: Optional[type] = None) -> DurablePromise[T]:
    """Return a durable promise (workflow handlers only)."""
    return current_context().promise(name, serde=serde, type_hint=type_hint)


# ── Invocation management ──


def cancel_invocation(invocation_id: str):
    """Cancel an invocation by id."""
    current_context().cancel_invocation(invocation_id)


def attach_invocation(
    invocation_id: str, serde: Serde[T] = DefaultSerde(), type_hint: Optional[type] = None
) -> RestateDurableFuture[T]:
    """Attach to an invocation by id."""
    return current_context().attach_invocation(invocation_id, serde=serde, type_hint=type_hint)
