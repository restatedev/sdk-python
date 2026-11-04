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
Session objects: Restate virtual objects whose state is a SQL database per
key. Its handlers run one at a time per key, its views beside them; each gets a
context of its own, made of the key, the request, the database and calls, and
nothing else of the SDK's context::

    notes = sessions.SessionObject("notes", schema=["CREATE TABLE notes (text TEXT NOT NULL)"])

    @notes.handler
    async def add(ctx: sessions.SessionContext, text: str) -> None:
        await ctx.db.execute("INSERT INTO notes (text) VALUES (?)", [text])

    @notes.view
    async def list(ctx: sessions.ViewContext) -> list[str]:
        return [row["text"] for row in await ctx.db.query("SELECT text FROM notes ORDER BY rowid")]
"""

from __future__ import annotations

import asyncio
import functools
import typing
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from restate import _preview
from restate.context import Request
from restate.exceptions import SdkInternalBaseException, SuspendedException, TerminalError
from restate.handler import RESTATE_UNIQUE_HANDLER_SYMBOL, Handler, handler_from_callable
from restate.object import VirtualObject
from restate.retry_policy import InvocationRetryPolicy
from restate.serde import BytesSerde, DefaultSerde, Serde
from restate.server_context import DisconnectedException, ServerInvocationContext
from restate.vm import SingleUnresolvedFuture
from restate.sessions import _selector
from restate.sessions._calls import CallFuture, Rpc, Send, send_now
from restate.sessions._database import Store
from restate.sessions._schema import Schema, Step
from restate.sessions._session import Attempt, Database, DatabaseReader, ObjectConfig, SqlHost, reader_of, with_attempt

F = typing.TypeVar("F", bound=Callable[..., Awaitable[Any]])

HEAD_KEY = "v22/head"
"""The key of the object database's head, its first read as it opens (shared-core's state format)."""

SQL_METADATA = {
    "dev.restate.tooling.journal.hidden.names": "sync",
    "dev.restate.tooling.journal.hidden.name.prefix": "v22/",
}
"""
The metadata every session object has: Restate's UI hides the database from
the journal view (its head, tail, manifests and packs in the object's state,
``v22/...``; and the ``sync`` run after its writes).
"""

Cancel = Callable[[str], None]
"""
Cancel invocation ``invocation_id`` (such as ``inv_1f…``), as Restate's admin
API does: made at once, and not awaited. Cancelling an invocation that already
ended does nothing, so a new attempt that cancels it again is harmless.
"""


@dataclass(frozen=True)
class SessionContext:
    """
    The context of a session object's handler. A handler runs in attempts:
    Restate starts a new one after a crash, a retryable error or a suspension,
    and each runs the handler from its first line, finding what the attempts
    before it committed. Its operations can be awaited together
    (``asyncio.gather``, ``asyncio.wait``).
    """

    key: str
    """The session's key."""
    request: Request
    """The request: its id (the same on every attempt), headers, and the attempt's."""
    db: Database
    """
    The key's database. Each statement outside a transaction is its own commit,
    and ``db.transaction()`` groups several; a commit is durable when its call
    returns. Calls run one at a time, in call order.
    """
    rpc: Rpc
    """Calls, typed by the handler called: ``await ctx.rpc(handler, key="a")(input)``."""
    send: Send
    """Sends, made at once; a transaction's (``tx.send``) are made once it commits."""
    cancel: Cancel
    """Cancel an invocation, by its id: see ``Cancel``."""
    _sdk: Any = field(default=None, repr=False, compare=False)
    """The SDK's context of the attempt, for ``cancelled`` and ``cancel_calls``."""
    _explicit_cancellation: bool = field(default=False, repr=False, compare=False)
    """Whether its handler has ``explicit_cancellation=True``."""


@dataclass(frozen=True)
class ViewContext:
    """
    The context of a session object's view: it runs beside the handlers, and
    reads the database as committed when its invocation started, whatever they
    commit meanwhile. It writes nothing, and sends nothing, but it can cancel an
    invocation: a handler of its own object's, say, as a Stop button does.
    """

    key: str
    request: Request
    db: DatabaseReader
    rpc: Rpc
    cancel: Cancel


class _ContextHost:
    """
    An attempt's session's host (``SqlHost``) over the SDK's context: its waits
    go through the selector, and its state commands are named after their keys,
    so that tooling can hide the database by name.
    """

    def __init__(self, context: ServerInvocationContext, shared: bool) -> None:
        self.context = context
        self.shared = shared
        self.service = context.handler.service_tag.name
        self.key = context.key()
        self._listeners: list[Callable[[], None]] = []
        self.selector = _selector.hook(context, self._notify_attempt_end)
        attempt_finished = context.on_attempt_finished

        def on_attempt_finished() -> None:
            self._notify_attempt_end()
            attempt_finished()

        context.on_attempt_finished = on_attempt_finished  # type: ignore[method-assign]

    @property
    def vm(self) -> Any:
        """
        The invocation's VM, which the database's store writes and reads
        directly (see ``_database.Store``); none with a journal codec, whose
        values only the context's state commands encode.
        """
        return None if self.context.journal_codec is not None else self.context.vm.vm

    def wait(self, handle: int) -> Awaitable[None]:
        """Done once the VM has the read whose handle is ``handle``: through the selector, as every wait."""
        return self.selector.wait(SingleUnresolvedFuture(handle))

    def get(self, key: str) -> Awaitable[bytes | None]:
        return self.context.get(key, serde=BytesSerde(), entry_name=key)

    def set(self, key: str, value: bytes) -> None:
        self.context.set(key, value, serde=BytesSerde(), entry_name=key)

    def clear(self, key: str) -> None:
        self.context.clear(key, entry_name=key)

    def state_keys(self) -> Awaitable[list[str]]:
        # Named under the format's prefix, which tooling hides.
        return self.context.state_keys(entry_name="v22/keys")

    def sync(self) -> Awaitable[Any]:
        return self.context.run_typed("sync", _nothing)

    def generic_call(
        self,
        service: str,
        handler: str,
        arg: bytes,
        key: str | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return self.context.generic_call(service, handler, arg, key, idempotency_key, headers)

    def generic_send(
        self,
        service: str,
        handler: str,
        arg: bytes,
        key: str | None = None,
        send_delay: timedelta | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return self.context.generic_send(service, handler, arg, key, send_delay, idempotency_key, headers)

    def attempt_end(self) -> BaseException | None:
        ended = self.selector.ended
        if ended is not None:
            return ended
        if self.context.request_finished_event.is_set():
            return SuspendedException()
        return None

    def on_attempt_end(self, listener: Callable[[], None]) -> Callable[[], None]:
        if self.attempt_end() is not None:
            listener()
            return lambda: None
        self._listeners.append(listener)

        def cancel() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return cancel

    def _notify_attempt_end(self) -> None:
        listeners, self._listeners = self._listeners, []
        for listener in listeners:
            listener()


async def _nothing() -> None:
    """The action of an empty run."""


def _cancel_of(host: Any) -> Cancel:
    """``Cancel`` over the SDK's context."""

    def cancel(invocation_id: str) -> None:
        # Not an id: a retry would fail the same way.
        if not invocation_id.startswith("inv"):
            raise TerminalError(f"not an invocation id: {invocation_id}")
        host.cancel_invocation(invocation_id)

    return cancel


@dataclass(frozen=True)
class _Entry:
    """A handler or view of a session object: its function, kind, and object's config."""

    fn: Callable[..., Awaitable[Any]]
    view: bool
    config: ObjectConfig
    explicit_cancellation: bool = False


def _context_of(entry: _Entry, host: SqlHost, request: Request, attempt: Attempt) -> SessionContext | ViewContext:
    """The context of an attempt of ``entry``."""
    rpc = Rpc(host)
    cancel = _cancel_of(host)
    if entry.view:
        return ViewContext(key=host.key, request=request, db=reader_of(attempt.db), rpc=rpc, cancel=cancel)
    return SessionContext(
        key=host.key,
        request=request,
        db=attempt.db,
        rpc=rpc,
        send=Send(send_now(host)),
        cancel=cancel,
        _sdk=getattr(host, "context", None),
        _explicit_cancellation=entry.explicit_cancellation,
    )


async def _attempt(entry: _Entry, host: SqlHost, request: Request, args: Sequence[Any]) -> Any:
    """One attempt of ``entry`` over ``host``: its own context and session, which ends with it."""
    attempt = Attempt(host, entry.config)
    attempt.open_now()
    context = _context_of(entry, host, request, attempt)
    return await with_attempt(lambda: entry.fn(context, *args), attempt)


def _sdk_handler(entry: _Entry) -> Callable[..., Awaitable[Any]]:
    """``entry`` as the SDK's handler: one loop drives each attempt's awaits (the selector), until it ends."""

    @functools.wraps(entry.fn)
    async def handler(ctx: ServerInvocationContext, *args: Any) -> Any:
        host = _ContextHost(ctx, shared=entry.view)
        return await _attempt(entry, host, ctx.request(), args)

    return handler


def _explicit_of(ctx: SessionContext, operation: str) -> Any:
    """The SDK's context behind ``ctx``, a handler's with ``explicit_cancellation=True``."""
    if not isinstance(ctx, SessionContext) or ctx._sdk is None:  # pylint: disable=protected-access
        raise TerminalError(f"{operation} takes a session object's ctx")
    if not ctx._explicit_cancellation:  # pylint: disable=protected-access
        raise TerminalError(f"{operation} takes the ctx of a handler with explicit_cancellation=True")
    return ctx._sdk  # pylint: disable=protected-access


async def cancelled(ctx: SessionContext) -> None:
    """
    Returns when Restate cancels the invocation of ``ctx``, a handler's with
    ``explicit_cancellation=True``, whose awaits are left as they are: for
    code that stops its own work then (a model's request, say)::

        async def stop_when_cancelled():
            await sessions.cancelled(ctx)
            await sessions.cancel_calls(ctx)

        asyncio.ensure_future(stop_when_cancelled())

    If the attempt ends first (the handler returned, or Restate suspended
    it), it raises ``asyncio.CancelledError``: a task that waits for it ends
    cancelled, quietly, as a task whose work is over does.
    """
    try:
        await _preview.cancellation(_explicit_of(ctx, "cancelled"))
    except (SdkInternalBaseException, DisconnectedException) as ended:
        raise asyncio.CancelledError() from ended


async def cancel_calls(ctx: SessionContext) -> list[str]:
    """
    Cancel the calls the attempt of ``ctx`` made and that have not completed,
    as a cancelled invocation's would be without explicit cancellation: a
    handler's with ``explicit_cancellation=True``. Returns their invocation ids.
    """
    return await _preview.cancel_previous_calls(_explicit_of(ctx, "cancel_calls"))


async def invoke(obj: SessionObject, name: str, host: SqlHost, request: Request, *args: Any) -> Any:
    """Run handler or view ``name`` of ``obj`` for one attempt over ``host``, a context of a test's own, without Restate."""
    entry = obj._entries.get(name)  # pylint: disable=protected-access
    if entry is None:
        raise ValueError(f"{obj.name}.{name} is no handler or view of a session object")
    return await _attempt(entry, host, request, args)


class SessionObject(VirtualObject):
    """
    A virtual object whose state is a SQL database per key.

    - ``schema``: the steps that make each key's database, in order, applied
      once per key by the first handler that uses it. A step that ran never
      changes: a change is a step of its own.
    - ``@obj.handler``: handlers run one at a time per key, each with a
      ``SessionContext``.
    - ``@obj.view``: views run beside the handlers, each with a
      ``ViewContext``: they read, and write nothing.

    A name is a handler or a view, not both. The other options are the SDK's
    virtual object's, but for its state options: a session object's state is
    its database's.
    """

    def __init__(
        self,
        name: str,
        *,
        schema: Sequence[Step] = (),
        description: str | None = None,
        metadata: dict[str, str] | None = None,
        inactivity_timeout: timedelta | None = None,
        abort_timeout: timedelta | None = None,
        journal_retention: timedelta | None = None,
        idempotency_retention: timedelta | None = None,
        ingress_private: bool | None = None,
        invocation_retry_policy: InvocationRetryPolicy | None = None,
    ) -> None:
        # TODO: start an invocation with only the database's head
        # (HEAD_KEY), once the SDK can say which keys to load eagerly: lazy
        # state loads none, so the open's first read is a round trip.
        super().__init__(
            name,
            description=description,
            metadata={**SQL_METADATA, **(metadata or {})},
            inactivity_timeout=inactivity_timeout,
            abort_timeout=abort_timeout,
            journal_retention=journal_retention,
            idempotency_retention=idempotency_retention,
            enable_lazy_state=True,
            ingress_private=ingress_private,
            invocation_retry_policy=invocation_retry_policy,
        )
        self.config = ObjectConfig(schema=Schema(name, schema) if schema else None)
        self._entries: dict[str, _Entry] = {}

    @typing.overload  # type: ignore[override]
    def handler(self, fn: F, /) -> F: ...  # pyright: ignore[reportIncompatibleMethodOverride]

    @typing.overload
    def handler(
        self,
        name: str | None = None,
        *,
        accept: str = "application/json",
        content_type: str = "application/json",
        input_serde: Serde[Any] = DefaultSerde(),
        output_serde: Serde[Any] = DefaultSerde(),
        metadata: dict[str, str] | None = None,
        inactivity_timeout: timedelta | None = None,
        abort_timeout: timedelta | None = None,
        journal_retention: timedelta | None = None,
        idempotency_retention: timedelta | None = None,
        ingress_private: bool | None = None,
        invocation_retry_policy: InvocationRetryPolicy | None = None,
        explicit_cancellation: bool = False,
    ) -> Callable[[F], F]: ...

    def handler(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        name: str | F | None = None,
        *,
        accept: str = "application/json",
        content_type: str = "application/json",
        input_serde: Serde[Any] = DefaultSerde(),
        output_serde: Serde[Any] = DefaultSerde(),
        metadata: dict[str, str] | None = None,
        inactivity_timeout: timedelta | None = None,
        abort_timeout: timedelta | None = None,
        journal_retention: timedelta | None = None,
        idempotency_retention: timedelta | None = None,
        ingress_private: bool | None = None,
        invocation_retry_policy: InvocationRetryPolicy | None = None,
        explicit_cancellation: bool = False,
    ) -> F | Callable[[F], F]:
        """
        A handler: it runs one at a time per key, and is the only one that
        writes. Its function takes a ``SessionContext``, and its input, if any::

            @notes.handler
            async def add(ctx: sessions.SessionContext, text: str) -> None:
                await ctx.db.execute("INSERT INTO notes (text) VALUES (?)", [text])

        With options, ``@notes.handler(name=..., ...)``: the SDK's handler's.
        With ``explicit_cancellation=True``, Restate's cancellation of its
        invocation fails no await, nor cancels its calls: the handler learns of
        it with ``sessions.cancelled(ctx)``, and decides what to stop (its calls,
        with ``sessions.cancel_calls(ctx)``).
        """
        return self._decorate(
            False,
            name,
            dict(
                explicit_cancellation=explicit_cancellation,
                accept=accept,
                content_type=content_type,
                input_serde=input_serde,
                output_serde=output_serde,
                metadata=metadata,
                inactivity_timeout=inactivity_timeout,
                abort_timeout=abort_timeout,
                journal_retention=journal_retention,
                idempotency_retention=idempotency_retention,
                ingress_private=ingress_private,
                invocation_retry_policy=invocation_retry_policy,
            ),
        )

    @typing.overload
    def view(self, fn: F, /) -> F: ...

    @typing.overload
    def view(
        self,
        name: str | None = None,
        *,
        accept: str = "application/json",
        content_type: str = "application/json",
        input_serde: Serde[Any] = DefaultSerde(),
        output_serde: Serde[Any] = DefaultSerde(),
        metadata: dict[str, str] | None = None,
        inactivity_timeout: timedelta | None = None,
        abort_timeout: timedelta | None = None,
        journal_retention: timedelta | None = None,
        idempotency_retention: timedelta | None = None,
        ingress_private: bool | None = None,
        invocation_retry_policy: InvocationRetryPolicy | None = None,
    ) -> Callable[[F], F]: ...

    def view(
        self,
        name: str | F | None = None,
        *,
        accept: str = "application/json",
        content_type: str = "application/json",
        input_serde: Serde[Any] = DefaultSerde(),
        output_serde: Serde[Any] = DefaultSerde(),
        metadata: dict[str, str] | None = None,
        inactivity_timeout: timedelta | None = None,
        abort_timeout: timedelta | None = None,
        journal_retention: timedelta | None = None,
        idempotency_retention: timedelta | None = None,
        ingress_private: bool | None = None,
        invocation_retry_policy: InvocationRetryPolicy | None = None,
    ) -> F | Callable[[F], F]:
        """
        A view: it runs beside the handlers, and reads the database as
        committed when its invocation started. Its function takes a
        ``ViewContext``, and its input, if any::

            @notes.view
            async def count(ctx: sessions.ViewContext) -> int:
                row = await ctx.db.get("SELECT count(*) AS n FROM notes")
                return row["n"] if row else 0

        A view that is still running after 16 commits of the handlers fails,
        and Restate retries it. With options, ``@notes.view(name=..., ...)``:
        the SDK's handler's.
        """
        return self._decorate(
            True,
            name,
            dict(
                accept=accept,
                content_type=content_type,
                input_serde=input_serde,
                output_serde=output_serde,
                metadata=metadata,
                inactivity_timeout=inactivity_timeout,
                abort_timeout=abort_timeout,
                journal_retention=journal_retention,
                idempotency_retention=idempotency_retention,
                ingress_private=ingress_private,
                invocation_retry_policy=invocation_retry_policy,
            ),
        )

    def _decorate(self, view: bool, name: str | F | None, options: dict[str, Any]) -> F | Callable[[F], F]:
        """The decorator of ``@obj.handler(...)``, or, bare (``@obj.handler``), the function it registers."""
        if callable(name):
            return self._register(view, None, options)(name)
        return self._register(view, name, options)

    def _register(self, view: bool, name: str | None, options: dict[str, Any]) -> Callable[[F], F]:
        def register(fn: F) -> F:
            if RESTATE_UNIQUE_HANDLER_SYMBOL in vars(fn):
                raise TypeError(
                    f"{self.name}.{fn.__name__} is an SDK handler: a session object's handlers are functions of its "
                    "context. Put journaled work in a restate.VirtualObject, Service or Workflow, and call it with ctx.rpc"
                )
            explicit = bool(options.get("explicit_cancellation"))
            sdk_options = {key: value for key, value in options.items() if key != "explicit_cancellation"}
            entry = _Entry(fn=fn, view=view, config=self.config, explicit_cancellation=explicit)
            registered = VirtualObject.handler(self, name, kind="shared" if view else "exclusive", **sdk_options)(
                _sdk_handler(entry)
            )
            sdk: Handler[Any, Any] = handler_from_callable(registered)
            # A later attempt makes different reads than a replayed journal
            # would have: each runs from the start, against the state as it is.
            sdk.explicit_recovery = True
            sdk.explicit_cancellation = explicit
            self._entries[sdk.name] = entry
            return typing.cast(F, registered)

        return register

    def with_store_wrapper(self, wrap: Callable[[Store], Store]) -> SessionObject:
        """Wrap the stores of this object's attempts with ``wrap``: fault injection, in tests."""
        self.config.wrap_store = wrap
        return self


__all__ = ["CallFuture", "Cancel", "SessionContext", "SessionObject", "ViewContext", "cancel_calls", "cancelled"]
