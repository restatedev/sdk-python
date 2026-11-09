#
#  Copyright (c) 2023-2026 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""Calls and sends from session objects: ``ctx.call``, ``ctx.send`` and ``tx.send``.

The target of a call or send is a handler function, passed together with its
key for a virtual object or a workflow, and the call is typed by the
handler's signature. A target known only at run time is addressed by name,
with ``generic_call`` and ``generic_send``::

    count = await ctx.call(increment, 5, key="a", idempotency_key=f"{ctx.request.id}:inc")
    ctx.send(record, entry, key="audit", delay=timedelta(hours=1))
    answer = await ctx.generic_call("Counter", "get", key="a")
"""

from __future__ import annotations

import typing
from collections.abc import Awaitable, Callable, Generator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from restate.context import RestateDurableCallFuture
from restate.exceptions import TerminalError
from restate.handler import Handler, handler_from_callable
from restate.serde import JsonSerde, Serde

I = typing.TypeVar("I")
O = typing.TypeVar("O")


@dataclass(frozen=True)
class GenericSend:
    """A send with its input already serialized, as ``ctx.send`` makes it or a transaction's outbox stores it."""

    service: str
    handler: str
    parameter: bytes
    key: str | None = None
    delay: timedelta | None = None
    idempotency_key: str | None = None
    headers: dict[str, str] | None = None


class CallHost(Protocol):
    """The part of the SDK's context that calls and sends use."""

    def generic_call(
        self,
        service: str,
        handler: str,
        arg: bytes,
        key: str | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> RestateDurableCallFuture[bytes]: ...

    def generic_send(
        self,
        service: str,
        handler: str,
        arg: bytes,
        key: str | None = None,
        send_delay: timedelta | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any: ...

    def attach_workflow(self, name: str, key: str) -> Awaitable[bytes]: ...


def _target(handler: Callable[..., Any], key: str | None) -> tuple[str, str, Serde[Any], Serde[Any]]:
    """Return the service name, handler name and serdes of ``handler``, checking ``key``.

    Raises ``TerminalError`` (since a retry would fail the same way) if
    ``handler`` is not a handler, if an object's or workflow's handler has no
    key, or if a service's handler has one.
    """
    try:
        target: Handler[Any, Any] = handler_from_callable(handler)
    except ValueError:
        raise TerminalError(f"{handler!r} is not a handler of a service, an object or a workflow") from None
    service = target.service_tag.name
    keyed = target.service_tag.kind != "service"
    if keyed and key is None:
        what = "a workflow" if target.service_tag.kind == "workflow" else "an object"
        raise TerminalError(f"{service} is {what}: call it with its key")
    if not keyed and key is not None:
        raise TerminalError(f"{service} is a service: call it without a key")
    return service, target.name, target.handler_io.input_serde, target.handler_io.output_serde


class CallFuture(RestateDurableCallFuture[O]):
    """A future for the result of a call, decoded with the handler's output serde.

    Await it for the result. ``invocation_id()`` returns the id of the call's
    invocation, and ``cancel_invocation()`` cancels it.
    """

    def __init__(self, call: RestateDurableCallFuture[bytes], serde: Serde[O]) -> None:
        super().__init__()
        self._call: RestateDurableCallFuture[bytes] = call
        self._serde: Serde[O] = serde

    async def _result(self) -> O:
        return typing.cast(O, self._serde.deserialize(await self._call))

    def __await__(self) -> Generator[Any, Any, O]:
        return self._result().__await__()

    async def invocation_id(self) -> str:
        return await self._call.invocation_id()

    async def cancel_invocation(self) -> None:
        await self._call.cancel_invocation()


class Calls:
    """A mixin that implements ``call`` and ``generic_call`` (see ``Caller`` in ``_api.py``) on top of ``_call_host``."""

    _call_host: CallHost

    @typing.overload
    def call(
        self,
        handler: Callable[[Any], Awaitable[O]],
        /,
        *,
        key: str | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> CallFuture[O]: ...

    @typing.overload
    def call(
        self,
        handler: Callable[[Any, I], Awaitable[O]],
        arg: I,
        /,
        *,
        key: str | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> CallFuture[O]: ...

    def call(
        self,
        handler: Callable[..., Awaitable[Any]],
        arg: Any = None,
        /,
        *,
        key: str | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> CallFuture[Any]:
        service, name, input_serde, output_serde = _target(handler, key)
        future = self._call_host.generic_call(service, name, input_serde.serialize(arg), key, idempotency_key, headers)
        return CallFuture(future, output_serde)

    def generic_call(
        self,
        service: str,
        handler: str,
        arg: Any = None,
        *,
        key: str | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
        input_serde: Serde[Any] = JsonSerde(),
        output_serde: Serde[Any] = JsonSerde(),
    ) -> CallFuture[Any]:
        parameter = input_serde.serialize(arg)
        future = self._call_host.generic_call(service, handler, parameter, key, idempotency_key, headers)
        return CallFuture(future, output_serde)


class Sends:
    """A mixin that implements ``send`` and ``generic_send`` (see ``Sender`` in ``_api.py``) by passing each send to ``_send_sink``.

    The sink either sends right away (``ctx.send``) or when a transaction
    commits (``tx.send``).
    """

    _send_sink: Callable[[GenericSend], None]

    @typing.overload
    def send(
        self,
        handler: Callable[[Any], Awaitable[Any]],
        /,
        *,
        key: str | None = None,
        delay: timedelta | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None: ...

    @typing.overload
    def send(
        self,
        handler: Callable[[Any, I], Awaitable[Any]],
        arg: I,
        /,
        *,
        key: str | None = None,
        delay: timedelta | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None: ...

    def send(
        self,
        handler: Callable[..., Awaitable[Any]],
        arg: Any = None,
        /,
        *,
        key: str | None = None,
        delay: timedelta | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        service, name, input_serde, _ = _target(handler, key)
        parameter = input_serde.serialize(arg)
        self._send_sink(GenericSend(service, name, parameter, key, delay, idempotency_key, headers))

    def generic_send(
        self,
        service: str,
        handler: str,
        arg: Any = None,
        *,
        key: str | None = None,
        delay: timedelta | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
        input_serde: Serde[Any] = JsonSerde(),
    ) -> None:
        parameter = input_serde.serialize(arg)
        self._send_sink(GenericSend(service, handler, parameter, key, delay, idempotency_key, headers))


def send_now(host: CallHost) -> Callable[[GenericSend], None]:
    """Return the sink of ``ctx.send``, which sends each send right away."""

    def sink(send: GenericSend) -> None:
        host.generic_send(
            send.service, send.handler, send.parameter, send.key, send.delay, send.idempotency_key, send.headers
        )

    return sink
