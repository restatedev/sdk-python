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
A session's database, through the native extension (``src/sessions.rs``):
SQLite-compatible SQL, kept in the object's state. The extension reads and
writes the state through a store, in the order it makes its requests; this
module orders the calls and runs transactions::

    db = await open("Ledger/a", store)
    async with db.transaction() as tx:
        await tx.execute("INSERT INTO transfers VALUES(?, ?, ?, ?)", [id, src, dst, amount])
    await db.save()  # ends the session, leaving its engine to the next one
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import typing
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol

from restate._internal import (  # pylint: disable=import-error,no-name-in-module
    SQL_CLEAR,
    SQL_GET,
    SQL_KEYS,
    SQL_SET,
    SQL_SYNC,
    SqlRefusedException,
    sql_open,
)

SqlValue = typing.Union[None, int, float, str, bytes]
"""An SQL value: ``None``, ``int`` (64 bits), ``float``, ``str``, or ``bytes`` (BLOB)."""

SqlParam = typing.Union[SqlValue, bool, bytearray, memoryview]
"""An SQL parameter: an ``SqlValue``, a ``bool`` (stored as 0 or 1), or a BLOB as any bytes-like object."""

Row = dict[str, SqlValue]
"""A row, by column name."""

Rows = list[list[SqlValue]]
"""Rows as lists, in column order."""


class SqlError(Exception):
    """
    A failure a retry would repeat: SQL the engine rejected (a syntax error, a
    constraint, a missing table), which undid just that statement and leaves
    the session usable; a parameter it cannot bind; or a call the session
    refused, which may have ended it (a ``COMMIT`` in a transaction's body, a
    transaction left open). A storage error, which ends the session, raises
    as it is.
    """


class Store(Protocol):
    """
    A key/value store, the object's state.

    ``set`` and ``clear`` return at once, and ``sync`` completes once every
    write before it is stored. Writes are stored in the order they were made:
    a crash keeps a prefix of the writes since the last sync and loses the
    rest. ``get`` issues its read when it is called, and returns the stored
    value or this request's own latest write (``None`` when absent), or an
    awaitable of it; so a plain ``async def get`` does not do, as its read
    would be issued only when it first runs, after writes made later.

    A store may also have ``keys()``: every key, as ``ctx.state_keys()``, or an
    awaitable of them. Then a commit that checkpoints deletes the packs no
    checkpoint needs any more; a store without it keeps them.
    """

    def get(self, key: str) -> bytes | None | Awaitable[bytes | None]: ...
    def set(self, key: str, value: bytes) -> None: ...
    def clear(self, key: str) -> None: ...
    def sync(self) -> None | Awaitable[None]: ...


async def open(namespace: str, store: Store, *, reader: bool = False) -> Connection:  # pylint: disable=redefined-builtin
    """
    Open the database of ``namespace`` over ``store``. One process-wide page
    cache serves every namespace. A ``reader`` reads beside the namespace's
    owner (a shared handler): the state as of open, whatever the owner commits
    meanwhile, until the owner's second checkpoint after it; it writes nothing.
    """
    boundary = _Boundary(store, asyncio.get_running_loop())
    try:
        native = await sql_open(namespace, boundary.dispatch, boundary.loop, boundary.lists_keys, reader)
    except Exception as error:
        if boundary.failed is not None:
            raise boundary.failed from error
        raise
    return Connection(native, boundary)


class _Boundary:
    """
    The store as the extension calls it, through ``dispatch`` on ``loop``. An
    error of the store's crosses the extension as text, which ends the
    session: the first is kept here, and the calls that fail on the session it
    ended raise it as it was raised (the SDK's suspension, say). A failed write
    cannot fail its caller, which has moved on: the next ``sync`` fails. And a
    commit's sync first runs what its transaction gave
    ``ConnectionTransaction.with_commit``: after the commit's writes, before
    anything is awaited.
    """

    def __init__(self, store: Store, loop: asyncio.AbstractEventLoop) -> None:
        self.store = store
        self.loop = loop
        self.lists_keys = callable(getattr(store, "keys", None))
        # The first error of the store's.
        self.failed: BaseException | None = None
        # What the next sync runs first: the committing transaction's.
        self.with_sync: Callable[[], None] | None = None
        # The first write that failed: every sync after it fails.
        self._write_failed: BaseException | None = None

    def _keep(self, error: BaseException) -> None:
        if self.failed is None:
            self.failed = error

    def dispatch(self, op: int, key: str, value: bytes | None, reply: Any) -> None:
        """One request of the extension's, on the event loop, in the order it made them."""
        if op in (SQL_SET, SQL_CLEAR):
            try:
                if op == SQL_SET:
                    self.store.set(key, typing.cast(bytes, value))
                else:
                    self.store.clear(key)
            except BaseException as error:  # pylint: disable=broad-exception-caught
                self._keep(error)
                if self._write_failed is None:
                    self._write_failed = error
            return
        try:
            if op == SQL_GET:
                result: Any = self.store.get(key)
            elif op == SQL_KEYS:
                result = self.store.keys()  # type: ignore[attr-defined]
            elif self._write_failed is not None:
                reply(None, str(self._write_failed))
                return
            else:
                run, self.with_sync = self.with_sync, None
                if run is not None:
                    run()
                result = self.store.sync()
        except BaseException as error:  # pylint: disable=broad-exception-caught
            self._keep(error)
            reply(None, str(error))
            return
        if inspect.isawaitable(result):
            self._answer(op, reply, result)
        else:
            reply(_reply_value(op, result))

    def _answer(self, op: int, reply: Any, awaitable: Awaitable[Any]) -> None:
        def done(future: asyncio.Future) -> None:
            if future.cancelled():
                self._keep(asyncio.CancelledError())
                reply(None, "the store's request was cancelled")
            elif future.exception() is not None:
                error = typing.cast(BaseException, future.exception())
                self._keep(error)
                reply(None, str(error))
            else:
                reply(_reply_value(op, future.result()))

        asyncio.ensure_future(awaitable, loop=self.loop).add_done_callback(done)


def _reply_value(op: int, result: Any) -> Any:
    """A request's answer as the extension reads it: a value, the keys as a list, or nothing for a sync."""
    if op == SQL_SYNC:
        return None
    if op == SQL_KEYS:
        return list(result)
    return result


def _objects(columns: list[str], rows: Rows) -> list[Row]:
    """The rows as dicts by column name; a column name twice is an ``SqlError``."""
    if len(set(columns)) != len(columns):
        twice = next(name for i, name in enumerate(columns) if name in columns[:i])
        raise SqlError(f"the column {twice} is named twice: alias one (AS), or read the rows with values()")
    return [dict(zip(columns, row)) for row in rows]


class _Sql:
    """SQL on a session of the extension: ``query``, ``get``, ``values`` and ``execute`` by way of ``query_columns``."""

    def __init__(self, native: Any, boundary: _Boundary) -> None:
        self._native = native
        self._boundary = boundary

    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        """The rows of one statement, with its column names: ``(columns, rows)``."""
        raise NotImplementedError

    async def query(self, sql: str, params: Sequence[SqlParam] | None = None) -> list[Row]:
        """
        The rows of one statement, each a dict by column name; parameters bind
        to a single statement. Columns are named as SQL names them (an
        expression needs ``AS``), and two of the same name are an
        ``SqlError``: alias them, or use ``values``.
        """
        return _objects(*await self.query_columns(sql, params))

    async def get(self, sql: str, params: Sequence[SqlParam] | None = None) -> Row | None:
        """The first row of ``query``, or ``None`` when there is none."""
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    async def values(self, sql: str, params: Sequence[SqlParam] | None = None) -> Rows:
        """The rows of one statement as lists, in column order: for adapters, and for columns that share a name."""
        return (await self.query_columns(sql, params))[1]

    async def execute(self, sql: str, params: Sequence[SqlParam] | None = None) -> None:
        """Run SQL (one or more statements), discarding rows; parameters only with a single statement."""
        await self.query_columns(sql, params)

    async def _request(self, call: Callable[[], Any]) -> Any:
        """
        ``call``'s result, or its ``_failure``: ``call`` makes the request, as
        the extension raises as it is called (arguments it cannot convert, and
        the error of a call that answers at once), and returns a value or an
        awaitable of it.
        """
        try:
            result = call()
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception as error:
            raise self._failure(error) from None

    def _failure(self, error: Exception) -> BaseException:
        """
        A request's ``error``: an ``SqlError`` if a retry would repeat it; the
        store's own error if that ended the session.
        """
        if isinstance(error, SqlError):
            return error
        failed = self._boundary.failed
        if failed is not None and not self._native.is_usable():
            return failed
        refused = isinstance(error, (SqlRefusedException, TypeError, ValueError))
        if refused or self._native.is_usable():
            sql_error = SqlError(str(error))
            sql_error.__cause__ = error
            return sql_error
        return error


# The transaction whose body is running, in that body's context (and the tasks
# it starts): the innermost, which knows the ones it runs inside.
_running: contextvars.ContextVar[ConnectionTransaction | None] = contextvars.ContextVar(
    "restate_sessions_running_transaction", default=None
)


class Connection(_Sql):
    """
    A session of a database: calls run one at a time, in call order, and a
    transaction holds the session until it commits or rolls back. Calls made
    meanwhile wait, but a call from inside the transaction's body is refused,
    as it would wait for that body: the body uses its ``tx``.
    """

    def __init__(self, native: Any, boundary: _Boundary) -> None:
        super().__init__(native, boundary)
        # FIFO: calls run in the order they were made.
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def _serial(self) -> AsyncIterator[None]:
        """Run the body once the calls before it have, unless it comes from the body of a transaction it would wait for."""
        running = _running.get()
        if running is not None and running.holds(self):
            raise SqlError("inside a transaction, use the transaction handle its body gets")
        async with self._lock:
            yield

    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        async with self._serial():
            return await self._request(lambda: self._native.query_columns(sql, params))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[ConnectionTransaction]:
        """
        One SQL transaction, as the body of an ``async with``::

            async with db.transaction() as tx:
                await tx.execute("UPDATE stock SET n = n - 1 WHERE id = ?", [7])

        It commits when the body ends, with the records it set, and rolls back
        when the body raises, with the body's own error, once the calls it made
        have completed. A statement in it that ends the transaction
        (``COMMIT``, ``ROLLBACK``) ends the session. A failing COMMIT rolls
        back.
        """
        async with self._serial():
            await self._request(self._native.begin)
            tx = ConnectionTransaction(self._native, self._boundary, self, _running.get())
            token = _running.set(tx)
            try:
                yield tx
            except BaseException:
                await tx._end()  # pylint: disable=protected-access
                await self._rollback()
                raise
            finally:
                _running.reset(token)
            records = await tx._end()  # pylint: disable=protected-access
            # Its commit's sync runs what the body gave `with_commit`; one that
            # wrote nothing synced nothing, and it runs after.
            self._boundary.with_sync = tx._commit_runs()  # pylint: disable=protected-access
            try:
                result = self._native.commit(records)
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                # A COMMIT that fails (a deferred constraint) leaves the
                # transaction open: without the rollback, the calls after it
                # would run inside it.
                await self._rollback()
                raise self._failure(error) from None
            finally:
                unsynced, self._boundary.with_sync = self._boundary.with_sync, None
            if unsynced is not None:
                unsynced()

    async def record(self, name: str) -> bytes:
        """The record ``name``, as the last commit that set it left it (see ``ConnectionTransaction.set_record``); empty if none did."""
        async with self._serial():
            return await self._request(lambda: self._native.record(name))

    def is_usable(self) -> bool:
        """Whether SQL can still run: an SQL error leaves the session usable, a storage error ends it."""
        return self._native.is_usable()

    async def save(self) -> None:
        """
        End the session after the calls before it, leaving its engine to the
        next one. It fails if they left a transaction open, or a failure ended
        the session.
        """
        async with self._serial():
            await self._request(self._native.save)

    async def close(self) -> None:
        """End the session now, whatever its state: queued calls fail."""
        await self._native.close()

    async def _rollback(self) -> None:
        """Roll back, unless a storage error ended the session: the caller raises its own error, which says why."""
        try:
            result = self._native.rollback()
            if inspect.isawaitable(result):
                await result
        except Exception:  # pylint: disable=broad-exception-caught
            pass


class ConnectionTransaction(_Sql):
    """
    SQL in one transaction, while its body runs, and the records it commits
    with. Its calls run one at a time, in call order, also when the body
    awaits several together, or none: each call made while it is open runs
    before its commit or rollback, so none outlives it.
    """

    def __init__(self, native: Any, boundary: _Boundary, connection: Connection, outer: ConnectionTransaction | None):
        super().__init__(native, boundary)
        self._connection = connection
        # The transaction whose body this one's runs in, if any: of another
        # connection, since a connection's own are refused.
        self._outer = outer
        self._records: dict[str, bytes] = {}
        self._with_commits: list[Callable[[], None]] = []
        self._lock = asyncio.Lock()
        self._ended = False

    @property
    def open(self) -> bool:
        """Whether it is open: its body runs, and holds its connection."""
        return not self._ended

    def holds(self, connection: Connection) -> bool:
        """Whether this transaction, or one it runs inside, is open and holds ``connection``."""
        if not self._ended and self._connection is connection:
            return True
        return self._outer is not None and self._outer.holds(connection)

    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        async with self._serial():
            return await self._request(lambda: self._native.query_columns(sql, params))

    def set_record(self, name: str, value: bytes) -> None:
        """
        Set the record ``name`` to ``value`` with this transaction, in the same
        write: a whole value kept beside the SQL, in no table, stored exactly
        when the transaction is, even if it changes no row. It stays until a
        later transaction sets it again; an empty value reads as never set. A
        name is at most 255 bytes, and not ``db``; a value at most 16 MiB.
        """
        self._refuse_ended()
        self._records[name] = bytes(value)

    async def record(self, name: str) -> bytes:
        """The record ``name`` as this transaction would commit it: as it set it, or as committed."""
        # As set when it is called, in call order with `set_record`.
        value = self._records.get(name)
        async with self._serial():
            if value is not None:
                return value
            return await self._request(lambda: self._native.record(name))

    def with_commit(self, run: Callable[[], None]) -> None:
        """
        Run ``run`` with the commit, right after its writes and before it waits
        for them to be stored: the store's host gets what ``run`` makes after
        the writes, and before anything that fails the commit's wait (Restate's
        sends, which it stores in order with the writes). Never if the
        transaction rolls back.
        """
        self._refuse_ended()
        self._with_commits.append(run)

    async def _end(self) -> dict[str, bytes]:
        """End it, refusing calls from now on, once the calls made before have completed; then the records it set."""
        self._ended = True
        async with self._lock:
            pass
        return dict(self._records)

    def _commit_runs(self) -> Callable[[], None] | None:
        """What runs with its commit, once it ended: see ``with_commit``."""
        runs = self._with_commits
        if not runs:
            return None

        def run_all() -> None:
            for run in runs:
                run()

        return run_all

    @asynccontextmanager
    async def _serial(self) -> AsyncIterator[None]:
        """Run the body, a call made while the transaction is open, once the calls before it have."""
        self._refuse_ended()
        async with self._lock:
            yield

    def _refuse_ended(self) -> None:
        if self._ended:
            raise SqlError("the transaction has ended")
