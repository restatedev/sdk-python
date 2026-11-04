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
import inspect
import typing
import weakref
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
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
from restate.sessions._api import Database, DatabaseReader, SessionContext, ViewContext
from restate.sessions._calls import CallHost, Calls, GenericSend, Sends, send_now
from restate.sessions._link import Link
from restate.sessions._schema import Schema, Step, steps_of
from restate.sessions._scope import SCOPE, Scope
from restate.sessions._selector import FutureSelector, hook
from restate.sessions._session import Attempt, ObjectConfig, SqlHost, reader_of, with_attempt
from restate.vm import SingleUnresolvedFuture

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


@dataclass(frozen=True, eq=False)
class AttemptContext(Calls, Sends):
    """``SessionContext`` (see ``_api.py``): one attempt's, made as it starts. Compared by identity."""

    key: str
    request: Request
    db: Database
    _call_host: CallHost
    _send_sink: Callable[[GenericSend], None]
    _cancel: Callable[[str], None]

    def cancel(self, invocation_id: str) -> None:
        self._cancel(invocation_id)


@dataclass(frozen=True, eq=False)
class AttemptViewContext(Calls):
    """``ViewContext`` (see ``_api.py``): one attempt's, made as it starts. Compared by identity."""

    key: str
    request: Request
    db: DatabaseReader
    _call_host: CallHost
    _cancel: Callable[[str], None]

    def cancel(self, invocation_id: str) -> None:
        self._cancel(invocation_id)


class _ContextHost:
    """
    An attempt's session's host (``SqlHost``) over the SDK's context: its waits
    go through the selector, and its state commands are named after their keys,
    so that tooling can hide the database by name.
    """

    def __init__(self, context: ServerInvocationContext, shared: bool) -> None:
        self.context: ServerInvocationContext = context
        self.shared: bool = shared
        self.service: str = context.handler.service_tag.name
        self.key: str = context.key()
        self._listeners: list[Callable[[], None]] = []
        self.selector: FutureSelector = hook(context, self._notify_attempt_end)
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

    def cancel_invocation(self, invocation_id: str) -> None:
        self.context.cancel_invocation(invocation_id)

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


def _cancel_of(host: SqlHost) -> Callable[[str], None]:
    """``cancel`` over the SDK's context."""

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
    definition: weakref.ref[SessionObject]
    """Its session object, weakly: the object's entries, which it holds, hold this."""
    explicit_cancellation: bool = False


ENTRIES: Link[dict[str, _Entry]] = Link("entries")
"""A session object's handlers and views, by name: for ``invoke``."""


def _context_of(entry: _Entry, host: SqlHost, request: Request, attempt: Attempt) -> SessionContext | ViewContext:
    """The context of an attempt of ``entry``."""
    cancel = _cancel_of(host)
    sdk = typing.cast("ServerInvocationContext | None", getattr(host, "context", None))
    scope = Scope(sdk=sdk, explicit_cancellation=entry.explicit_cancellation, definition=entry.definition())
    if entry.view:
        view = AttemptViewContext(
            key=host.key, request=request, db=reader_of(attempt.db), _call_host=host, _cancel=cancel
        )
        return SCOPE.on(view, scope)
    context = AttemptContext(
        key=host.key,
        request=request,
        db=attempt.db,
        _call_host=host,
        _send_sink=send_now(host),
        _cancel=cancel,
    )
    return SCOPE.on(context, scope)


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


def _explicit_of(ctx: SessionContext, operation: str) -> ServerInvocationContext:
    """The SDK's context behind ``ctx``, a handler's with ``explicit_cancellation=True``."""
    scope = SCOPE.of(ctx)
    if scope is None or scope.sdk is None:
        raise TerminalError(f"{operation} takes a session object's ctx")
    if not scope.explicit_cancellation:
        raise TerminalError(f"{operation} takes the ctx of a handler with explicit_cancellation=True")
    return scope.sdk


async def cancelled(ctx: SessionContext) -> None:
    """
    Returns when Restate cancels the invocation of ``ctx``, a handler's with
    ``explicit_cancellation=True``, whose awaits are left as they are: for
    code that stops its own work then (a model's request, say)::

        async def stop_when_cancelled():
            await internal.cancelled(ctx)
            await internal.cancel_calls(ctx)

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


def definition_of(ctx: SessionContext | ViewContext) -> SessionObject | None:
    """
    The session object whose handler or view ``ctx`` is the context of: what
    a library keeps per object (an agent's options, say) lives on it.
    """
    scope = SCOPE.of(ctx)
    definition = scope.definition if scope is not None else None
    return definition if isinstance(definition, SessionObject) else None


async def invoke(obj: SessionObject, name: str, host: SqlHost, request: Request, *args: Any) -> Any:
    """Run handler or view ``name`` of ``obj`` for one attempt over ``host``, a context of a test's own, without Restate."""
    entry = (ENTRIES.of(obj) or {}).get(name)
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
        super().__init__(
            name,
            description=description,
            metadata={**SQL_METADATA, **(metadata or {})},
            inactivity_timeout=inactivity_timeout,
            abort_timeout=abort_timeout,
            journal_retention=journal_retention,
            idempotency_retention=idempotency_retention,
            # An invocation starts with the database's head only, its first read as it opens: the rest it
            # reads as it needs it (its tail entries and packs change keys with each flush: only the head
            # can be named). As sdk-sessions' sqlOptions; restate-server 1.8 or later.
            state_preload=[HEAD_KEY],
            ingress_private=ingress_private,
            invocation_retry_policy=invocation_retry_policy,
        )
        self._config: ObjectConfig = ObjectConfig(schema=Schema(name, schema) if steps_of(schema) else None)
        ENTRIES.on(self, {})

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r})"

    def _taken(self, name: str) -> str:
        """Why ``name`` is taken already, for the error of a second handler of that name."""
        return f"{self.name} has a handler or view named {name!r} already: give this one another name"

    @typing.overload  # type: ignore[override]
    def handler(self, fn: F, /) -> F: ...

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
        it with ``internal.cancelled(ctx)``, and decides what to stop (its calls,
        with ``internal.cancel_calls(ctx)``), of ``restate.sessions.internal``.
        """
        return self._decorate(
            False,
            name,
            {
                "explicit_cancellation": explicit_cancellation,
                "accept": accept,
                "content_type": content_type,
                "input_serde": input_serde,
                "output_serde": output_serde,
                "metadata": metadata,
                "inactivity_timeout": inactivity_timeout,
                "abort_timeout": abort_timeout,
                "journal_retention": journal_retention,
                "idempotency_retention": idempotency_retention,
                "ingress_private": ingress_private,
                "invocation_retry_policy": invocation_retry_policy,
            },
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
            {
                "accept": accept,
                "content_type": content_type,
                "input_serde": input_serde,
                "output_serde": output_serde,
                "metadata": metadata,
                "inactivity_timeout": inactivity_timeout,
                "abort_timeout": abort_timeout,
                "journal_retention": journal_retention,
                "idempotency_retention": idempotency_retention,
                "ingress_private": ingress_private,
                "invocation_retry_policy": invocation_retry_policy,
            },
        )

    def _decorate(self, view: bool, name: str | F | None, options: dict[str, Any]) -> F | Callable[[F], F]:
        """The decorator of ``@obj.handler(...)``, or, bare (``@obj.handler``), the function it registers."""
        if callable(name):
            return self._register(view, None, options)(name)
        return self._register(view, name, options)

    def _attempt_fn(self, fn: Callable[..., Awaitable[Any]], view: bool) -> Callable[..., Awaitable[Any]]:
        """
        What an attempt of handler (or view, with ``view``) ``fn`` runs: ``fn``
        itself; a subclass's may wrap it (an agent's turns a model provider's
        refusal into a terminal error).
        """
        del view
        return fn

    def _register(self, view: bool, name: str | None, options: dict[str, Any]) -> Callable[[F], F]:
        def register(fn: F) -> F:
            if RESTATE_UNIQUE_HANDLER_SYMBOL in vars(fn):
                what = f"{self.name}.{fn.__name__} is a handler already, of the SDK's or of a session object's."
                instead = (
                    "A session object's handlers are functions of its context, of their own; put journaled work "
                    "in a restate.VirtualObject, Service or Workflow, and call it with ctx.call."
                )
                raise TypeError(f"{what} {instead}")
            if not inspect.iscoroutinefunction(inspect.unwrap(fn)):
                kind = "view" if view else "handler"
                raise TypeError(f"{self.name}.{fn.__name__}: a {kind} must be a coroutine function (async def)")
            chosen = name or fn.__name__
            if chosen in self.handlers:
                raise ValueError(self._taken(chosen))
            explicit = bool(options.get("explicit_cancellation"))
            sdk_options = {key: value for key, value in options.items() if key != "explicit_cancellation"}
            entry = _Entry(
                fn=self._attempt_fn(fn, view),
                view=view,
                config=self._config,
                definition=weakref.ref(self),
                explicit_cancellation=explicit,
            )
            registered = VirtualObject.handler(self, name, kind="shared" if view else "exclusive", **sdk_options)(
                _sdk_handler(entry)
            )
            sdk: Handler[Any, Any] = handler_from_callable(registered)
            # A later attempt makes different reads than a replayed journal
            # would have: each runs from the start, against the state as it is.
            sdk.explicit_recovery = True
            sdk.explicit_cancellation = explicit
            entries = ENTRIES.of(self)
            assert entries is not None
            entries[sdk.name] = entry
            # The function itself, marked as the handler it is: `ctx.call(fn, ...)`
            # and the client find it, and a test calls it with a context of
            # its own (a fake of the protocols).
            vars(fn)[RESTATE_UNIQUE_HANDLER_SYMBOL] = sdk
            return fn

        return register


__all__ = ["SessionObject", "cancel_calls", "cancelled", "definition_of"]
