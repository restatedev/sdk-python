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
"""
Calls and sends from a session object's handler: ``ctx.rpc`` and ``ctx.send``,
and a transaction's ``tx.send``. A target is a handler (with its key for an
object or a workflow), typed by its signature; or, for one known only at run
time, its names, with ``generic``. Options go with the target: what it returns
takes only the input::

    count = await ctx.rpc(increment, key="a", idempotency_key=f"{ctx.request.id}:inc")(5)
    ctx.send(record, key="audit", delay=timedelta(hours=1))(entry)
    answer = await ctx.rpc.generic("Counter", "get", key="a")
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
    """A send, its input serialized: as ``ctx.send`` makes it, or a transaction's outbox keeps it."""

    service: str
    handler: str
    parameter: bytes
    key: str | None = None
    delay: timedelta | None = None
    idempotency_key: str | None = None
    headers: dict[str, str] | None = None


class CallHost(Protocol):
    """What calls and sends need of the SDK's context."""

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


def _target(handler: Callable[..., Any], key: str | None) -> tuple[str, str, Serde[Any], Serde[Any]]:
    """
    The service, handler name and serdes of ``handler``, called with ``key``. An
    object's or a workflow's handler needs its key, and a service's has none:
    a ``TerminalError`` otherwise, since a retry would fail the same way.
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
    """The result of a call, as its output serde reads it: awaitable, with the call's invocation id."""

    def __init__(self, call: RestateDurableCallFuture[bytes], serde: Serde[O]) -> None:
        super().__init__()
        self._call = call
        self._serde = serde

    async def _result(self) -> O:
        return typing.cast(O, self._serde.deserialize(await self._call))

    def __await__(self) -> Generator[Any, Any, O]:
        return self._result().__await__()

    async def invocation_id(self) -> str:
        return await self._call.invocation_id()

    async def cancel_invocation(self) -> None:
        await self._call.cancel_invocation()


class Rpc:
    """
    Calls: ``ctx.rpc(handler, key=...)`` returns a function of the handler's
    input, if it takes one, which makes the call and returns its result's
    awaitable. A call is made when that function is called, awaited or not.
    """

    def __init__(self, host: CallHost) -> None:
        self._host = host

    @typing.overload
    def __call__(
        self,
        handler: Callable[[Any], Awaitable[O]],
        key: str | None = None,
        *,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Callable[[], CallFuture[O]]: ...

    @typing.overload
    def __call__(
        self,
        handler: Callable[[Any, I], Awaitable[O]],
        key: str | None = None,
        *,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Callable[[I], CallFuture[O]]: ...

    def __call__(
        self,
        handler: Callable[..., Awaitable[Any]],
        key: str | None = None,
        *,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Callable[..., CallFuture[Any]]:
        """
        A caller of ``handler``, with ``key`` for an object's or a workflow's.

        ``idempotency_key`` is the same on every attempt, so that a new attempt
        gets the first call's result; without one, every attempt makes the call
        again, the one that resumes a suspension too. Restate scopes it to the
        target handler.
        """
        service, name, input_serde, output_serde = _target(handler, key)

        def call(arg: Any = None) -> CallFuture[Any]:
            parameter = input_serde.serialize(arg)
            future = self._host.generic_call(service, name, parameter, key, idempotency_key, headers)
            return CallFuture(future, output_serde)

        return call

    def generic(
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
        """
        Call a target known only at run time, by its names: untyped, and JSON
        unless serdes are given. ``key`` is the caller's to give, for an
        object or a workflow.
        """
        future = self._host.generic_call(service, handler, input_serde.serialize(arg), key, idempotency_key, headers)
        return CallFuture(future, output_serde)


class Send:
    """
    Sends, as ``Rpc``'s calls: ``ctx.send(handler, key=...)`` returns a function
    of the handler's input, if it takes one, which makes the send at once
    (``ctx.send``), or records it for its transaction's commit (``tx.send``).
    """

    def __init__(self, sink: Callable[[GenericSend], None]) -> None:
        self._sink = sink

    @typing.overload
    def __call__(
        self,
        handler: Callable[[Any], Awaitable[Any]],
        key: str | None = None,
        *,
        delay: timedelta | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Callable[[], None]: ...

    @typing.overload
    def __call__(
        self,
        handler: Callable[[Any, I], Awaitable[Any]],
        key: str | None = None,
        *,
        delay: timedelta | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Callable[[I], None]: ...

    def __call__(
        self,
        handler: Callable[..., Awaitable[Any]],
        key: str | None = None,
        *,
        delay: timedelta | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Callable[..., None]:
        """
        A sender to ``handler``, with ``key`` for an object's or a workflow's;
        ``delay`` is how long after the send (for ``tx.send``, after the
        commit) the target runs. See ``Rpc`` for ``idempotency_key``.
        """
        service, name, input_serde, _ = _target(handler, key)

        def send(arg: Any = None) -> None:
            parameter = input_serde.serialize(arg)
            self._sink(GenericSend(service, name, parameter, key, delay, idempotency_key, headers))

        return send

    def generic(
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
        """Send to a target known only at run time, by its names: untyped, and JSON unless a serde is given."""
        self._sink(GenericSend(service, handler, input_serde.serialize(arg), key, delay, idempotency_key, headers))


def send_now(host: CallHost) -> Callable[[GenericSend], None]:
    """The sink of ``ctx.send``: each send is made at once."""

    def sink(send: GenericSend) -> None:
        host.generic_send(
            send.service, send.handler, send.parameter, send.key, send.delay, send.idempotency_key, send.headers
        )

    return sink
