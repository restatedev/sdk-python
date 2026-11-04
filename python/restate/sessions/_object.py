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
"""Session objects: Restate virtual objects whose state is a SQL database per key.

Handlers run one at a time per key, and views run alongside them. Each gets a
context of its own that holds the key, the request, the database, and calls
and sends, and nothing else from the SDK's context::

    notes = sessions.SessionObject("notes", migrations=["CREATE TABLE notes (text TEXT NOT NULL)"])


    @notes.handler
    async def add(ctx: sessions.SessionContext, text: str) -> None:
        await ctx.db.execute("INSERT INTO notes (text) VALUES (?)", [text])


    @notes.view(name="list")
    async def list_notes(ctx: sessions.ViewContext) -> list[str]:
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
from restate.sessions._schema import Migration, Schema, steps_of
from restate.sessions._scope import SCOPE, Scope
from restate.sessions._selector import FutureSelector, hook
from restate.sessions._session import Attempt, ObjectConfig, SqlHost, reader_of, with_attempt
from restate.vm import SingleUnresolvedFuture

F = typing.TypeVar("F", bound=Callable[..., Awaitable[Any]])

HEAD_KEY = "v22/head"
"""The state key of the database's head, the first thing read when it opens."""

SQL_METADATA = {
    "dev.restate.tooling.journal.hidden.names": "sync",
    "dev.restate.tooling.journal.hidden.name.prefix": "v22/",
}
"""Metadata added to every session object.

It tells Restate's UI to hide the database from the journal view: its head,
tail, manifests and packs (state keys under ``v22/``), and the ``sync`` run
after its writes.
"""


@dataclass(frozen=True, eq=False)
class AttemptContext(Calls, Sends):
    """The ``SessionContext`` of one attempt, created when the attempt starts; compared by identity."""

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
    """The ``ViewContext`` of one attempt, created when the attempt starts; compared by identity."""

    key: str
    request: Request
    db: DatabaseReader
    _call_host: CallHost
    _cancel: Callable[[str], None]

    def cancel(self, invocation_id: str) -> None:
        self._cancel(invocation_id)


class _ContextHost:
    """A ``SqlHost`` over the SDK's context, for one attempt.

    Its waits go through the selector, and its state commands are named after
    their keys, so that tooling can hide the database by name.
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
        """The invocation's VM, which the database reads and writes directly (see ``_database.Store``).

        It is ``None`` when a journal codec is configured, since only the
        context's state commands apply the codec.
        """
        return None if self.context.journal_codec is not None else self.context.vm.vm

    def wait(self, handle: int) -> Awaitable[None]:
        """Wait, through the selector, until the VM has the result of the read ``handle``."""
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
        # Named under the state format's prefix, which tooling hides.
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
    """Do nothing; the action of an empty run."""


def _cancel_of(host: SqlHost) -> Callable[[str], None]:
    """Return the context's ``cancel``, implemented on ``host``."""

    def cancel(invocation_id: str) -> None:
        # Terminal: a retry would fail the same way.
        if not invocation_id.startswith("inv"):
            raise TerminalError(f"not an invocation id: {invocation_id}")
        host.cancel_invocation(invocation_id)

    return cancel


@dataclass(frozen=True)
class _Entry:
    """A registered handler or view of a session object."""

    fn: Callable[..., Awaitable[Any]]
    view: bool
    config: ObjectConfig
    definition: weakref.ref[SessionObject]
    """The session object, held weakly because the object holds its entries."""
    explicit_cancellation: bool = False


ENTRIES: Link[dict[str, _Entry]] = Link("entries")
"""A session object's handlers and views, by name, for ``invoke``."""


def _context_of(entry: _Entry, host: SqlHost, request: Request, attempt: Attempt) -> SessionContext | ViewContext:
    """Create the context for an attempt of ``entry``."""
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
    """Run one attempt of ``entry`` over ``host``, with a context and database session of its own."""
    attempt = Attempt(host, entry.config)
    attempt.open_now()
    context = _context_of(entry, host, request, attempt)
    return await with_attempt(lambda: entry.fn(context, *args), attempt)


def _sdk_handler(entry: _Entry) -> Callable[..., Awaitable[Any]]:
    """Wrap ``entry`` as an SDK handler whose attempts drive their awaits through a selector."""

    @functools.wraps(entry.fn)
    async def handler(ctx: ServerInvocationContext, *args: Any) -> Any:
        host = _ContextHost(ctx, shared=entry.view)
        return await _attempt(entry, host, ctx.request(), args)

    return handler


def _explicit_of(ctx: SessionContext, operation: str) -> ServerInvocationContext:
    """Return the SDK's context behind ``ctx``, which must be a handler's with ``explicit_cancellation=True``."""
    scope = SCOPE.of(ctx)
    if scope is None or scope.sdk is None:
        raise TerminalError(f"{operation} takes a session object's ctx")
    if not scope.explicit_cancellation:
        raise TerminalError(f"{operation} takes the ctx of a handler with explicit_cancellation=True")
    return scope.sdk


async def cancelled(ctx: SessionContext) -> None:
    """Wait until Restate cancels the invocation of ``ctx``.

    For handlers declared with ``explicit_cancellation=True``, whose awaits
    are not interrupted by a cancellation, so that they can stop their own
    work (a model request, for example)::

        async def stop_when_cancelled():
            await internal.cancelled(ctx)
            await internal.cancel_calls(ctx)


        asyncio.ensure_future(stop_when_cancelled())

    Args:
        ctx: The context of a handler with ``explicit_cancellation=True``.

    Raises:
        asyncio.CancelledError: If the attempt ends first (the handler
            returned, or Restate suspended it), so that a task waiting on this
            ends quietly, as cancelled.
        TerminalError: If ``ctx`` is not the context of a handler with
            ``explicit_cancellation=True``.
    """
    try:
        await _preview.cancellation(_explicit_of(ctx, "cancelled"))
    except (SdkInternalBaseException, DisconnectedException) as ended:
        raise asyncio.CancelledError() from ended


async def cancel_calls(ctx: SessionContext) -> list[str]:
    """Cancel the calls made by the attempt of ``ctx`` that have not completed yet.

    This is what a cancellation does automatically for handlers without
    explicit cancellation.

    Args:
        ctx: The context of a handler with ``explicit_cancellation=True``.

    Returns:
        The invocation ids of the cancelled calls.

    Raises:
        TerminalError: If ``ctx`` is not the context of a handler with
            ``explicit_cancellation=True``.
    """
    return await _preview.cancel_previous_calls(_explicit_of(ctx, "cancel_calls"))


def definition_of(ctx: SessionContext | ViewContext) -> SessionObject | None:
    """Return the session object whose handler or view ``ctx`` belongs to, or ``None``.

    Libraries use it to find what they keep per object, such as an agent's
    options.
    """
    scope = SCOPE.of(ctx)
    definition = scope.definition if scope is not None else None
    return definition if isinstance(definition, SessionObject) else None


async def invoke(obj: SessionObject, name: str, host: SqlHost, request: Request, *args: Any) -> Any:
    """Run one attempt of the handler or view ``name`` of ``obj`` on ``host``, without Restate.

    This is for tests, which provide a ``SqlHost`` of their own.

    Args:
        obj: The session object.
        name: The name of the handler or view.
        host: The host that stores state and makes calls.
        request: The request the context reports.
        *args: The handler's input, if it takes one.

    Returns:
        The handler's result.

    Raises:
        ValueError: If ``obj`` has no handler or view named ``name``.
    """
    entry = (ENTRIES.of(obj) or {}).get(name)
    if entry is None:
        raise ValueError(f"{obj.name}.{name} is no handler or view of a session object")
    return await _attempt(entry, host, request, args)


class SessionObject(VirtualObject):
    """A virtual object whose state is a SQL database per key.

    A session object offers:

    - A SQL database per key, ``ctx.db``, built by ``migrations``.
    - Handlers (``@obj.handler``), which run one at a time per key and may
      write to the database.
    - Views (``@obj.view``), which run alongside the handlers and only read.
    - Calls and sends to other handlers (``ctx.call``, ``ctx.send``), and
      sends that commit with a transaction (``tx.send``).

    ::

        notes = sessions.SessionObject("notes", migrations=["CREATE TABLE notes (text TEXT NOT NULL)"])

    Each name is either a handler or a view, not both. The remaining options
    are those of ``restate.VirtualObject``, minus its state options, since a
    session object's state is its database.

    Args:
        name: The object's name, as Restate serves it.
        migrations: The migrations that build each key's database, in order.
            They are applied once per key, by the first handler that uses the
            database. Never change a migration that has run; add a new one.
        description: The object's description, shown in Restate's UI.
        metadata: The object's metadata.
        inactivity_timeout: Restate's inactivity timeout for its handlers.
        abort_timeout: Restate's abort timeout for its handlers.
        journal_retention: How long Restate keeps an invocation's journal.
        idempotency_retention: How long Restate keeps the result of an
            invocation with an idempotency key.
        ingress_private: Whether only other services may call it.
        invocation_retry_policy: How Restate retries a failed invocation.

    Raises:
        TypeError: If ``migrations`` is a string rather than a list.
    """

    def __init__(
        self,
        name: str,
        *,
        migrations: Sequence[Migration] = (),
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
            # Preload only the database's head, the first thing read when it opens; the rest is read on
            # demand (the tail entries and packs change keys with each flush, so only the head has a fixed
            # name). Needs restate-server 1.8 or later.
            state_preload=[HEAD_KEY],
            ingress_private=ingress_private,
            invocation_retry_policy=invocation_retry_policy,
        )
        self._config: ObjectConfig = ObjectConfig(schema=Schema(name, migrations) if steps_of(migrations) else None)
        ENTRIES.on(self, {})

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r})"

    def _reserved(self, name: str) -> str | None:
        """Return why ``name`` is reserved for a built-in handler, if it is (none are, for a plain session object)."""
        del name
        return None

    def _taken(self, name: str) -> str:
        """Return the error message for registering ``name`` a second time."""
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
        """Register a handler, used bare (``@obj.handler``) or with options (``@obj.handler(...)``).

        Handlers run one at a time per key, and only handlers can write to the
        database. The function takes a ``SessionContext`` and its input, if
        any, and stays a plain function, so a test can call it directly::

            @notes.handler
            async def add(ctx: sessions.SessionContext, text: str) -> None:
                await ctx.db.execute("INSERT INTO notes (text) VALUES (?)", [text])

        The options are those of ``restate.VirtualObject.handler``, plus
        ``explicit_cancellation``.

        Args:
            name: The handler's name. Defaults to the function's name.
            explicit_cancellation: If true, cancelling the invocation neither
                fails its awaits nor cancels its calls. The handler learns of
                the cancellation with ``internal.cancelled(ctx)`` and decides
                what to stop, for example its calls with
                ``internal.cancel_calls(ctx)`` (from
                ``restate.sessions.internal``).

        Returns:
            The function itself, or a decorator if called with options.

        Raises:
            TypeError: If the function is not an ``async def``, or is already
                a handler.
            ValueError: If the object already has a handler or view of that
                name.
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
        """Register a view, used bare (``@obj.view``) or with options (``@obj.view(...)``).

        Views run alongside the handlers and read the database as it was
        committed when the view started. The function takes a
        ``ViewContext`` and its input, if any::

            @notes.view
            async def count(ctx: sessions.ViewContext) -> int:
                row = await ctx.db.get("SELECT count(*) AS n FROM notes")
                return row["n"] if row else 0

        A view still running after the handlers have committed 16 times
        fails, and Restate retries it. The options are those of
        ``restate.VirtualObject.handler``.

        Args:
            name: The view's name. Defaults to the function's name.

        Returns:
            The function itself, or a decorator if called with options.

        Raises:
            TypeError: If the function is not an ``async def``, or is already
                a handler.
            ValueError: If the object already has a handler or view of that
                name.
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
        """Return the decorator for ``@obj.handler(...)``, or register the function directly for a bare ``@obj.handler``."""
        if callable(name):
            return self._register(view, None, options)(name)
        return self._register(view, name, options)

    def _attempt_fn(self, fn: Callable[..., Awaitable[Any]], view: bool) -> Callable[..., Awaitable[Any]]:
        """Return the function an attempt of ``fn`` runs; subclasses may wrap it (agents do)."""
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
            reserved = self._reserved(chosen)
            if reserved is not None:
                raise ValueError(reserved)
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
            # Attempts are not replayed: each runs from the start, against the
            # current state, so it may read differently from earlier attempts.
            sdk.explicit_recovery = True
            sdk.explicit_cancellation = explicit
            entries = ENTRIES.of(self)
            assert entries is not None
            entries[sdk.name] = entry
            # Return the function itself, marked as a handler: `ctx.call(fn, ...)`
            # and the client can find it, and a test can call it with a fake
            # context.
            vars(fn)[RESTATE_UNIQUE_HANDLER_SYMBOL] = sdk
            return fn

        return register


__all__ = ["SessionObject", "cancel_calls", "cancelled", "definition_of"]
