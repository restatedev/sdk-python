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
import json
import typing
import weakref
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from restate._internal import PyExplicitRecoveryVM, PyInput  # pylint: disable=no-name-in-module
from restate.context import Request
from restate.exceptions import TerminalError
from restate.handler import RESTATE_UNIQUE_HANDLER_SYMBOL, Handler, handler_from_callable
from restate.object import VirtualObject
from restate.retry_policy import InvocationRetryPolicy
from restate.serde import DefaultSerde, Serde
from restate.server_types import ReceiveChannel, Send, header_to_binary
from restate.sessions._api import Database, DatabaseReader, SessionContext, ViewContext
from restate.sessions._calls import CallHost, Calls, GenericSend, Sends, send_now, send_to
from restate.sessions._driver import AttemptDriver, AttemptFinished, VmHost
from restate.sessions._link import Link
from restate.sessions._schema import Migration, Schema, steps_of
from restate.sessions._scope import SCOPE, Scope
from restate.sessions._session import Attempt, ObjectConfig, SqlHost, reader_of, with_attempt

F = typing.TypeVar("F", bound=Callable[..., Awaitable[Any]])

HEAD_KEY = "v22/head"
"""The state key of the database's head, the first thing read when it opens."""

HIDDEN_NAMES = "dev.restate.tooling.journal.hidden.names"
"""The metadata key of the names of the journal entries that Restate's UI hides: a comma-separated list."""

SQL_METADATA = {
    HIDDEN_NAMES: "sync",
    "dev.restate.tooling.journal.hidden.name.prefix": "v22/",
}
"""Metadata added to every session object.

It tells Restate's UI to hide the database from the journal view: its head,
tail, manifests and packs, and the writes that only ask for an ack (state keys
under ``v22/``), and the ``sync`` runs of earlier versions.
"""


def hide_entries(*names: str) -> dict[str, str]:
    """Return the metadata that has Restate's UI hide the journal entries named ``names``, besides the database's.

    Pass it as a session object's metadata, and name the calls, sends or runs
    to hide with one of ``names``. Restate's UI still shows them on request.
    """
    return {HIDDEN_NAMES: ",".join([SQL_METADATA[HIDDEN_NAMES], *names])}


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
    driver = host.driver if isinstance(host, VmHost) else None
    scope = Scope(driver=driver, explicit_cancellation=entry.explicit_cancellation, definition=entry.definition())
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
    """The SDK's handler for ``entry``: it has ``entry``'s signature, for the SDK, but Restate's attempts run ``_runner``'s."""

    @functools.wraps(entry.fn)
    async def handler(*_: Any) -> Any:
        raise RuntimeError("a session object's attempts run on their own VM (see _runner)")

    return handler


async def _invoke(
    entry: _Entry,
    handler: Handler[Any, Any],
    driver: AttemptDriver,
    request: PyInput,
    attempt_headers: dict[str, str],
    codec: Any,
) -> bytes:
    """Run the attempt's handler on its input; its output, serialized."""
    body = bytes(request.input)
    data = body
    if codec is not None:
        try:
            data = await codec.decode(data)
        except Exception as e:
            raise TerminalError(message="Failed to decode input using journal value codec", status_code=400) from e
    args: tuple[Any, ...] = ()
    if handler.arity == 2:
        try:
            args = (handler.handler_io.input_serde.deserialize(data),)
        except Exception as e:
            raise TerminalError(message=f"Unable to parse an input argument. {e}") from e
    host = VmHost(driver, service=handler.service_tag.name, key=request.key, shared=entry.view, codec=codec)
    context_request = Request(
        id=request.invocation_id,
        headers={header.key: header.value for header in request.headers},
        attempt_headers=attempt_headers,
        body=body,
        attempt_finished_event=AttemptFinished(driver.finished),
        scope=request.scope,
        limit_key=request.limit_key,
        idempotency_key=request.idempotency_key,
    )
    output = handler.handler_io.output_serde.serialize(await _attempt(entry, host, context_request, args))
    if codec is not None:
        output = codec.encode(bytes(output))
    return bytes(output)


def _runner(entry: _Entry) -> Callable[..., Awaitable[None]]:
    """Return what runs an attempt of ``entry`` on Restate, on its own VM (see ``_driver``)."""

    async def run(
        handler: Handler[Any, Any],
        *,
        request_headers: list[tuple[str, str]],
        attempt_headers: dict[str, str],
        receive: ReceiveChannel,
        send: Send,
        bidirectional: bool,
        server_headers: list[tuple[bytes, bytes]],
        journal_codec: Any = None,
    ) -> None:
        vm = PyExplicitRecoveryVM(request_headers, bidirectional)
        head = vm.get_response_head()
        await send(
            {
                "type": "http.response.start",
                "status": head.status_code,
                "headers": [*header_to_binary(head.headers), *server_headers],
                "trailers": False,
            }
        )
        driver = AttemptDriver(vm, send, receive, entry.explicit_cancellation)
        try:
            request = await driver.start()
            if request is not None:
                await driver.run(lambda: _invoke(entry, handler, driver, request, attempt_headers, journal_codec))
        except asyncio.CancelledError:
            driver.abandon()
            raise
        await driver.leave()

    return run


def _explicit_of(ctx: SessionContext, operation: str) -> AttemptDriver:
    """Return the attempt behind ``ctx``, which must be a handler's with ``explicit_cancellation=True``."""
    scope = SCOPE.of(ctx)
    if scope is None or scope.driver is None:
        raise TerminalError(f"{operation} takes a session object's ctx")
    if not scope.explicit_cancellation:
        raise TerminalError(f"{operation} takes the ctx of a handler with explicit_cancellation=True")
    return scope.driver


async def cancelled(ctx: SessionContext) -> None:
    """Wait until Restate cancels the invocation of ``ctx``.

    For handlers declared with ``explicit_cancellation=True``, whose awaits
    are not interrupted by a cancellation, so that they can stop their own
    work (a model request, for example)::

        async def stop_when_cancelled():
            await internal.cancelled(ctx)
            await internal.cancel_calls(ctx)


        asyncio.ensure_future(stop_when_cancelled())

    It watches for the cancellation, and holds nothing: awaiting it neither
    suspends the invocation nor tells Restate that the handler waits. A
    cancellation reaches the handler as soon as Restate sends it, or at the
    start of the next attempt if an earlier one received it.

    Args:
        ctx: The context of a handler with ``explicit_cancellation=True``.

    Raises:
        asyncio.CancelledError: If the attempt ends first (the handler
            returned, or Restate suspended it), so that a task waiting on this
            ends quietly, as cancelled.
        TerminalError: If ``ctx`` is not the context of a handler with
            ``explicit_cancellation=True``.
    """
    # The attempt's end cancels the cancellation's future.
    await asyncio.shield(_explicit_of(ctx, "cancelled").cancellation)


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
    return await _explicit_of(ctx, "cancel_calls").cancel_calls()


def _host_of(ctx: SessionContext) -> CallHost:
    if not isinstance(ctx, AttemptContext):
        raise TypeError(f"{ctx!r} is not the context of a session object's handler")
    return ctx._call_host  # pyright: ignore[reportPrivateUsage] (this module's own class)


async def start(
    ctx: SessionContext, handler: Callable[..., Awaitable[Any]], arg: Any = None, *, key: str | None = None
) -> str:
    """Start an invocation of ``handler`` with ``arg`` by a send, made right away as ``ctx.send`` does, and return its id.

    For libraries that wait for what they sent (``attach_invocation``). A
    send to a workflow's main handler whose run of that id started already
    starts nothing: its invocation id is that run's.

    Raises:
        TypeError: If ``ctx`` is not the context of a session object's handler.
    """
    sent = send_to(_host_of(ctx), handler, arg, key)
    return typing.cast(str, await sent.invocation_id())


class Annotation:
    """Some work of a handler, timed in the journal as a run that nothing executes or awaits.

    Restate's UI shows it as any run: its name, when it started and ended,
    and the value or failure it ended with. A handler of a session object is
    never replayed, so the run need not be made again on a later attempt. Its
    end is not waited for: the attempt goes on as Restate stores it.

    A run its attempt left without an end (a crash, say) is failed by the
    next attempt.
    """

    def __init__(self, host: VmHost | None = None, handle: int | None = None) -> None:
        self._host: VmHost | None = host
        self._handle: int | None = handle
        # The trace, which stays a parent after it ends.
        self._trace: int | None = handle

    def end(self, summary: Any = None) -> None:
        """End the work, with ``summary`` (as JSON) as the run's value. It does nothing if it has ended."""
        self._close(json.dumps(summary, default=str).encode() if summary is not None else b"", None)

    def fail(self, message: str) -> None:
        """End the work as failed: the run fails with ``message``. It does nothing if it has ended."""
        self._close(b"", message)

    def _close(self, value: bytes, failure: str | None) -> None:
        handle, self._handle = self._handle, None
        if handle is not None and self._host is not None:
            self._host.close_annotation(handle, value, failure)


def annotate(ctx: SessionContext, name: str, *, parent: Annotation | None = None) -> Annotation:
    """Start timing some work of the handler of ``ctx`` (a model request, say), as a run named ``name``.

    The work is part of ``parent``'s, if given, or else of the attempt's. End
    it with ``end`` or ``fail``. Outside a session object's handler on Restate
    (with a test's host, say), nothing is timed.
    """
    host = ctx._call_host if isinstance(ctx, AttemptContext) else None  # pyright: ignore[reportPrivateUsage]
    if not isinstance(host, VmHost):
        return Annotation()
    parent_trace = parent._trace if parent is not None else None  # pyright: ignore[reportPrivateUsage]
    return Annotation(host, host.open_annotation(name, parent_trace))


def attach_invocation(ctx: SessionContext, invocation_id: str) -> Awaitable[bytes]:
    """Wait for the invocation ``invocation_id`` to end, and return its result, serialized.

    Raises:
        TypeError: If ``ctx`` is not the context of a session object's handler.
    """
    return _host_of(ctx).attach_invocation(invocation_id)


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
                fails its awaits nor cancels its calls (without it, a
                cancellation cancels the calls under way and fails the awaits
                pending, or else the next one: see ``Database``). The handler
                learns of the cancellation with ``internal.cancelled(ctx)``
                and decides what to stop, for example its calls with
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
            entry = _Entry(
                fn=self._attempt_fn(fn, view),
                view=view,
                config=self._config,
                definition=weakref.ref(self),
                explicit_cancellation=bool(options.get("explicit_cancellation")),
            )
            # Attempts are not replayed: each runs from the start, on a VM of its
            # own (see _driver), against the current state, so it may read
            # differently from earlier attempts.
            registered = VirtualObject.handler(
                self, name, kind="shared" if view else "exclusive", explicit_recovery=True, **options
            )(_sdk_handler(entry))
            sdk: Handler[Any, Any] = handler_from_callable(registered)
            sdk.attempt_runner = _runner(entry)
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
