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
The API of session objects, as handlers and views code against it: the
contexts, the database and its transactions, calls and sends. Protocols, so
that what a handler sees is what is declared here, and a test can give a
handler a fake of its own; the implementations are this package's private
classes (``_object.py``, ``_session.py``, ``_calls.py``).
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

Cancel = Callable[[str], None]
"""
Cancel invocation ``invocation_id`` (such as ``inv_1f…``), as Restate's admin
API does: made at once, and not awaited. Cancelling an invocation that already
ended does nothing, so a new attempt that cancels it again is harmless.
"""


# --- The database ---------------------------------------------------------------------------------


class DatabaseReader(Protocol):
    """Reads of a database, or of one transaction of it: a view's ``ctx.db``."""

    async def query(self, sql: str, params: Sequence[SqlParam] | None = None) -> list[Row]:
        """
        The rows of one statement, each a dict by column name; parameters bind
        to a single statement. Columns are named as SQL names them (an
        expression needs ``AS``), and two of the same name are an error: alias
        them, or use ``values``.
        """
        ...

    async def get(self, sql: str, params: Sequence[SqlParam] | None = None) -> Row | None:
        """The first row of ``query``, or ``None`` when there is none."""
        ...

    async def values(self, sql: str, params: Sequence[SqlParam] | None = None) -> Rows:
        """The rows of one statement as lists, in column order: for adapters, and for columns that share a name."""
        ...

    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        """The rows of one statement as lists, with the column names of the statement: ``(columns, rows)``."""
        ...


class DatabaseWriter(DatabaseReader, Protocol):
    """SQL: a database, or one transaction of it."""

    async def execute(self, sql: str, params: Sequence[SqlParam] | None = None) -> None:
        """Run SQL (one or more statements), discarding rows; parameters only with a single statement."""
        ...


class Transaction(DatabaseWriter, Protocol):
    """One transaction of a ``Database``: its SQL, and the sends it commits with."""

    @property
    def send(self) -> Send:
        """
        Sends made right after the transaction commits, and never if it rolls
        back. Each has the idempotency key it is given, or else one of the
        transaction (``transaction/<id>/<index>``), so a send an attempt makes
        again is dropped.
        """
        ...


class Database(DatabaseWriter, Protocol):
    """
    The session's database, ``ctx.db``, for the whole handler: each statement
    outside ``transaction()`` is its own transaction. Calls run one at a time,
    in call order. A commit is durable once its call returns.
    """

    def transaction(self) -> typing.AsyncContextManager[Transaction]:
        """
        One transaction, as the body of an ``async with``::

            async with ctx.db.transaction() as tx:
                await tx.execute("UPDATE accounts SET balance = balance - ? WHERE id = ?", [amount, src])
                tx.send(deposit, key=dst)(amount)

        It commits when the body ends, and rolls back when the body raises. It
        holds the database: other calls wait until it ends, and a call to the
        database from inside the body is refused, as it would wait for the body.
        So the body uses the ``tx`` it gets, which is invalid once the body has
        ended.
        """
        ...


# --- Calls and sends ------------------------------------------------------------------------------


class Rpc(Protocol):
    """
    Calls: ``ctx.rpc(handler, key=...)`` returns a function of the handler's
    input, if it takes one, which makes the call and returns its result's
    awaitable. A call is made when that function is called, awaited or not::

        count = await ctx.rpc(increment, key="a", idempotency_key=f"{ctx.request.id}:inc")(5)
    """

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
        A caller of ``handler``, with ``key`` for an object's or a workflow's
        (a ``TerminalError`` otherwise, as a retry would fail the same way).

        ``idempotency_key`` is the same on every attempt, so that a new attempt
        gets the first call's result; without one, every attempt makes the call
        again, the one that resumes a suspension too. Restate scopes it to the
        target handler.
        """
        ...

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
        ...


class Send(Protocol):
    """
    Sends, as ``Rpc``'s calls: ``ctx.send(handler, key=...)`` returns a function
    of the handler's input, if it takes one, which makes the send at once
    (``ctx.send``), or records it for its transaction's commit (``tx.send``)::

        ctx.send(record, key="audit", delay=timedelta(hours=1))(entry)
    """

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
        ...

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
        ...


# --- The contexts ---------------------------------------------------------------------------------


class SessionContext(Protocol):
    """
    The context of a session object's handler. A handler runs in attempts:
    Restate starts a new one after a crash, a retryable error or a suspension,
    and each runs the handler from its first line, finding what the attempts
    before it committed. Its operations can be awaited together
    (``asyncio.gather``, ``asyncio.wait``).
    """

    @property
    def key(self) -> str:
        """The session's key."""
        ...

    @property
    def request(self) -> Request:
        """The request: its id (the same on every attempt), headers, and the attempt's."""
        ...

    @property
    def db(self) -> Database:
        """
        The key's database. Each statement outside a transaction is its own
        commit, and ``db.transaction()`` groups several; a commit is durable
        when its call returns. Calls run one at a time, in call order.
        """
        ...

    @property
    def rpc(self) -> Rpc:
        """Calls, typed by the handler called: ``await ctx.rpc(handler, key="a")(input)``."""
        ...

    @property
    def send(self) -> Send:
        """Sends, made at once; a transaction's (``tx.send``) are made once it commits."""
        ...

    @property
    def cancel(self) -> Cancel:
        """Cancel an invocation, by its id: see ``Cancel``."""
        ...


class ViewContext(Protocol):
    """
    The context of a session object's view: it runs beside the handlers, and
    reads the database as committed when its invocation started, whatever they
    commit meanwhile. It writes nothing, and sends nothing, but it can cancel an
    invocation: a handler of its own object's, say, as a Stop button does.
    """

    @property
    def key(self) -> str:
        """The session's key."""
        ...

    @property
    def request(self) -> Request:
        """The request: its id, headers, and the attempt's."""
        ...

    @property
    def db(self) -> DatabaseReader:
        """The key's database as committed when the view's invocation started: reads only."""
        ...

    @property
    def rpc(self) -> Rpc:
        """Calls, typed by the handler called."""
        ...

    @property
    def cancel(self) -> Cancel:
        """Cancel an invocation, by its id: see ``Cancel``."""
        ...
