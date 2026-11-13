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
"""The API that session objects' handlers and views code against.

The contexts, the database and its transactions, and calls and sends are
declared as protocols: what a handler sees is exactly what is declared here,
and a test can pass a handler a fake of its own. The implementations are
private classes of this package (``_object.py``, ``_session.py``,
``_calls.py``).
"""

from __future__ import annotations

import typing
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Any, Protocol

from restate.context import Request
from restate.serde import JsonSerde, Serde
from restate.sessions._calls import CallFuture
from restate.sessions._database import Row, Rows, SqlParam

I = typing.TypeVar("I")
O = typing.TypeVar("O")


# --- The database ---------------------------------------------------------------------------------


class DatabaseReader(Protocol):
    """Read access to a database, or to one transaction of it; a view's ``ctx.db``."""

    async def query(self, sql: str, params: Sequence[SqlParam] | None = None) -> list[Row]:
        """Run one statement and return its rows, each a dict keyed by column name.

        Columns are named as SQL names them, so an expression needs ``AS``;
        two columns of the same name raise ``SqlError`` (alias one, or use
        ``values``)::

            rows = await ctx.db.query("SELECT text FROM notes WHERE done = ?", [False])
        """
        ...

    async def get(self, sql: str, params: Sequence[SqlParam] | None = None) -> Row | None:
        """Return the first row of ``query``, or ``None`` if there is none."""
        ...

    async def values(self, sql: str, params: Sequence[SqlParam] | None = None) -> Rows:
        """Run one statement and return its rows as lists, in column order."""
        ...

    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        """Run one statement and return its column names and its rows as lists: ``(columns, rows)``."""
        ...


class DatabaseWriter(DatabaseReader, Protocol):
    """Read and write access to a database, or to one transaction of it."""

    async def execute(self, sql: str, params: Sequence[SqlParam] | None = None) -> None:
        """Run SQL and discard any rows.

        ``sql`` may hold several statements, but parameters bind only to a
        single one.
        """
        ...


# --- Calls and sends ------------------------------------------------------------------------------


class Caller(Protocol):
    """Calls to other handlers, through Restate."""

    @typing.overload
    def call(
        self,
        handler: Callable[[Any], Awaitable[O]],
        /,
        *,
        key: str | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
        name: str | None = None,
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
        name: str | None = None,
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
        name: str | None = None,
    ) -> CallFuture[Any]:
        """Call ``handler`` with ``arg``, and return a future of its result.

        The call is typed by the handler's own signature, and is made right
        away, whether or not it is awaited::

            balance = await ctx.call(deposit, 10, key="alice", idempotency_key=f"{ctx.request.id}:deposit")

        Args:
            handler: A handler of a service, a virtual object (a session
                object's too) or a workflow.
            arg: Its input, if it takes one.
            key: The object's or workflow's key; a service takes none.
            idempotency_key: The same on every attempt, so that a retried
                attempt gets the first call's result instead of calling again.
            headers: Headers to send with the call.
            name: The name of the call's journal entry, which Restate's UI
                shows, and which the object's metadata can hide (see
                ``dev.restate.tooling.journal.hidden.names``).

        Raises:
            TerminalError: If ``handler`` is not a handler, or ``key`` is
                missing for an object or a workflow, or given for a service.
        """
        ...

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
        name: str | None = None,
    ) -> CallFuture[Any]:
        """Call a handler known only at run time, by its service's and its own name.

        Untyped: the input and result are JSON unless serdes are given.
        """
        ...


class Sender(Protocol):
    """Sends to other handlers, through Restate: one-way calls, which do not wait for the target."""

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
        name: str | None = None,
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
        name: str | None = None,
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
        name: str | None = None,
    ) -> None:
        """Send ``arg`` to ``handler``, without waiting for it to run::

            ctx.send(remind, note, key=ctx.key, delay=timedelta(days=1))

        Args:
            handler: A handler of a service, a virtual object (a session
                object's too) or a workflow.
            arg: Its input, if it takes one.
            key: The object's or workflow's key; a service takes none.
            delay: How long to wait before the target runs.
            idempotency_key: The same on every attempt, so that a retried
                attempt does not send again.
            headers: Headers to send with the request.
            name: The name of the send's journal entry, as for ``call``.

        Raises:
            TerminalError: If ``handler`` is not a handler, or ``key`` is
                missing for an object or a workflow, or given for a service.
        """
        ...

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
        name: str | None = None,
    ) -> None:
        """Send to a handler known only at run time, by its service's and its own name (JSON unless a serde is given)."""
        ...


# --- Transactions ---------------------------------------------------------------------------------


class Transaction(DatabaseWriter, Sender, Protocol):
    """One transaction of a ``Database``: its SQL, and the sends it commits with.

    ``tx.send`` sends right after the transaction commits, and never if it
    rolls back. Each committed send goes out once: if the process fails after
    the commit but before the send, the next attempt makes it, under the same
    idempotency key, and Restate drops any duplicate.

    A retried attempt that runs the transaction's body again commits a new
    transaction, with sends of its own. To send once across attempts, give the
    send an ``idempotency_key`` that is the same on every attempt (one made
    from ``ctx.request.id``, say), or skip the body when its rows show that it
    already ran.
    """


class Database(DatabaseWriter, Protocol):
    """A session's database, ``ctx.db``.

    Each statement outside ``transaction()`` is a transaction of its own,
    durable once its call returns. Calls run one at a time, in call order.
    """

    def transaction(self) -> typing.AsyncContextManager[Transaction]:
        """Start a transaction, for the body of an ``async with``::

            async with ctx.db.transaction() as tx:
                await tx.execute("UPDATE accounts SET balance = balance - ? WHERE id = ?", [amount, ctx.key])
                tx.send(deposit, amount, key=to)

        It commits when the body ends, and rolls back if the body raises. It
        holds the database until then: use the ``tx`` it gives, as a call to
        ``ctx.db`` inside the body is refused.
        """
        ...


# --- The contexts ---------------------------------------------------------------------------------


class SessionContext(Caller, Sender, Protocol):
    """The context of a session object's handler.

    A handler runs in attempts: after a crash, a retryable error or a
    suspension, Restate starts a new attempt, which runs the handler from the
    start and finds what earlier attempts committed. Its operations may be
    awaited together, with ``asyncio.gather`` or ``asyncio.wait``.
    """

    @property
    def key(self) -> str:
        """The key of this session."""
        ...

    @property
    def request(self) -> Request:
        """The request: its id (the same on every attempt), its headers, and the attempt's."""
        ...

    @property
    def db(self) -> Database:
        """The key's database."""
        ...

    def cancel(self, invocation_id: str) -> None:
        """Cancel the invocation ``invocation_id`` (such as ``"inv_1f…"``).

        The cancellation is sent right away, and not awaited. Cancelling an
        invocation that has ended does nothing, so a retried attempt may
        cancel it again.
        """
        ...


class ViewContext(Caller, Protocol):
    """The context of a session object's view.

    A view runs beside the handlers, and reads the database as committed
    when it started, whatever they commit meanwhile. It writes and sends
    nothing, but it may call other handlers, and cancel an invocation (a
    handler of its own object's, as a Stop button does).
    """

    @property
    def key(self) -> str:
        """The key of this session."""
        ...

    @property
    def request(self) -> Request:
        """The request: its id, its headers, and the attempt's."""
        ...

    @property
    def db(self) -> DatabaseReader:
        """The key's database, as committed when the view started: read-only."""
        ...

    def cancel(self, invocation_id: str) -> None:
        """Cancel the invocation ``invocation_id``: see ``SessionContext.cancel``."""
        ...
