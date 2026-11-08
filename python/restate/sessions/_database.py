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
"""The SQL database of a session, backed by the native extension (``src/sessions.rs``).

The database speaks SQLite-compatible SQL and is stored in the object's
state. The extension reads and writes that state through a ``Store``, in the
order it issues its requests; this module serializes calls and runs
transactions::

    db = await open("Ledger/a", store)
    async with db.transaction() as tx:
        await tx.execute("INSERT INTO transfers VALUES(?, ?, ?, ?)", [id, src, dst, amount])
    await db.save()  # end the session, handing its engine to the next one
"""

from __future__ import annotations

import abc
import asyncio
import contextlib
import contextvars
import inspect
import typing
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol

from restate._internal import (  # pylint: disable=import-error,no-name-in-module
    SQL_CLEAR,
    SQL_GET,
    SQL_KEYS,
    SQL_SET,
    SQL_SYNC,
    SQL_WAIT,
    PySqlDatabase,
    SqlRefusedException,
    SqlReply,
    sql_open,
)
from restate.exceptions import TerminalError

SqlValue = None | int | float | str | bytes
"""A value read from the database: ``None``, ``int`` (64-bit), ``float``, ``str`` or ``bytes`` (a BLOB)."""

SqlParam = SqlValue | bool | bytearray | memoryview
"""A query parameter: any ``SqlValue``, a ``bool`` (stored as 0 or 1), or any bytes-like object (a BLOB)."""

Row = dict[str, SqlValue]
"""A row, as a dict that maps column names to values."""

Rows = list[list[SqlValue]]
"""Rows as lists of values, in column order."""


class SqlError(TerminalError):
    """A SQL statement failed, and retrying it would fail the same way.

    Raised for SQL the engine rejects (a syntax error, a constraint violation,
    a missing table), which undoes just that statement; for a parameter it
    cannot bind; and for a call the database refuses (a ``COMMIT`` inside a
    transaction's body, say). It is a ``TerminalError``: uncaught, it fails
    the invocation instead of being retried. Catch it to handle, say, a
    ``UNIQUE`` violation::

        try:
            await ctx.db.execute("INSERT INTO users (email) VALUES (?)", [email])
        except sessions.SqlError:
            return "taken"

    Storage errors are not ``SqlError``: they are retried.
    """


class Store(Protocol):
    """The key/value store that holds a database: the object's state.

    ``set`` and ``clear`` return immediately, and ``sync`` completes once
    every earlier write is stored. Writes are stored in the order they were
    made, so a crash keeps some prefix of the writes since the last sync and
    loses the rest. ``get`` must issue its read when it is called, and return
    the stored value (or this request's own latest write, or ``None`` if
    absent), either directly or as an awaitable. A plain ``async def get``
    does not qualify: its read would only be issued when the coroutine first
    runs, after any writes made in the meantime.

    Optional members:

    - ``keys()`` returns every key, as ``ctx.state_keys()`` does, or an
      awaitable of them. With it, a commit that writes a checkpoint deletes
      the packs no checkpoint needs any more; without it, they are kept.
    - ``vm`` is the invocation's VM (``restate._internal.PyVM``), for a store
      over a Restate context. The extension then reads and writes the VM
      directly instead of calling ``get``, ``set`` and ``clear``, and calls
      ``wait(handle)`` (an awaitable that completes once the VM has the read
      for ``handle``) only when it must wait for a read.
    - ``database_name`` names a database of the key other than the object's
      own (see ``_session.database``).
    """

    def get(self, key: str, /) -> bytes | None | Awaitable[bytes | None]: ...
    def set(self, key: str, value: bytes, /) -> None: ...
    def clear(self, key: str, /) -> None: ...
    def sync(self) -> None | Awaitable[None]: ...


async def open(namespace: str, store: Store, *, reader: bool = False) -> Connection:  # pylint: disable=redefined-builtin
    """Open the database of ``namespace``, stored in ``store``.

    All namespaces share one process-wide page cache. A ``reader`` (for a
    shared handler) reads alongside the namespace's owner and never writes:
    it sees the state as of when it was opened, whatever the owner commits
    meanwhile, until the owner's second checkpoint after that.
    """
    boundary = _Boundary(store, asyncio.get_running_loop())
    vm = getattr(store, "vm", None)
    name = getattr(store, "database_name", None) if vm is not None else None
    try:
        native = await _settled(sql_open(namespace, boundary.dispatch, boundary.lists_keys, reader, vm, name))
    except Exception as error:
        if boundary.failed is not None:
            raise boundary.failed from error
        raise
    return Connection(native, boundary)


T = typing.TypeVar("T")


async def _settled(answer: T | Awaitable[T]) -> T:
    """Return ``answer``, awaiting it first if it is awaitable."""
    if inspect.isawaitable(answer):
        return await answer
    return answer


class _Boundary:
    """An adapter that serves the extension's requests, which arrive through ``dispatch``, from a ``Store``.

    Requests are handled on the event loop's thread, in the order the
    extension makes them. A store error reaches the extension only as text,
    and ends the session, so the first one is kept here: the calls that then
    fail re-raise the original exception (the SDK's suspension, for example).
    A failed write cannot fail its caller, which has moved on, so the next
    ``sync`` fails instead. Before a commit's sync, the callbacks the
    transaction registered with ``ConnectionTransaction.with_commit`` run,
    after the commit's writes and before anything is awaited.
    """

    def __init__(self, store: Store, loop: asyncio.AbstractEventLoop) -> None:
        self.store: Store = store
        self.loop: asyncio.AbstractEventLoop = loop
        # The store's optional methods (see `Store`).
        self._keys: Callable[[], Sequence[str] | Awaitable[Sequence[str]]] | None = getattr(store, "keys", None)
        self._wait: Callable[[int], Awaitable[None]] | None = getattr(store, "wait", None)
        self.lists_keys: bool = callable(self._keys)
        # The store's first error.
        self.failed: BaseException | None = None
        # What the next sync runs first: the committing transaction's callbacks.
        self.with_sync: Callable[[], None] | None = None
        # The first failed write; every sync after it fails.
        self._write_failed: BaseException | None = None

    def _keep(self, error: BaseException) -> None:
        if self.failed is None:
            self.failed = error

    def dispatch(self, op: int, key: Any, value: bytes | None, reply: SqlReply | None) -> None:
        """Handle one request of the extension; ``key`` is a store key, or a VM handle for ``SQL_WAIT``."""
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
        # Every request except a write has a reply (src/sessions.rs).
        assert reply is not None
        try:
            if op == SQL_GET:
                result: object = self.store.get(key)
            elif op == SQL_KEYS:
                assert self._keys is not None
                result = self._keys()
            elif op == SQL_WAIT:
                assert self._wait is not None
                result = self._wait(key)
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

    def _answer(self, op: int, reply: SqlReply, awaitable: Awaitable[object]) -> None:
        def done(future: asyncio.Future[object]) -> None:
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


def _reply_value(op: int, result: object) -> object:
    """Convert a request's result to what the extension expects: a value, a list of keys, or ``None``."""
    if op in (SQL_SYNC, SQL_WAIT):
        return None
    if op == SQL_KEYS:
        return list(typing.cast("Sequence[str]", result))
    return result


def objects_of(columns: list[str], rows: Rows) -> list[Row]:
    """Return ``rows`` as dicts keyed by column name, raising ``SqlError`` if a column name repeats."""
    if len(set(columns)) != len(columns):
        twice = next(name for i, name in enumerate(columns) if name in columns[:i])
        raise SqlError(f"the column {twice} is named twice: alias one (AS), or read the rows with values()")
    return [dict(zip(columns, row, strict=True)) for row in rows]


class _Sql(abc.ABC):
    """Base class for SQL access: ``query``, ``get``, ``values`` and ``execute``, built on ``query_columns``."""

    def __init__(self, native: PySqlDatabase, boundary: _Boundary) -> None:
        self._native: PySqlDatabase = native
        self._boundary: _Boundary = boundary

    @abc.abstractmethod
    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        """Run one statement and return its column names and rows: ``(columns, rows)``."""

    async def query(self, sql: str, params: Sequence[SqlParam] | None = None) -> list[Row]:
        """Run one statement and return its rows, each a dict keyed by column name.

        Columns are named as SQL names them, so an expression needs ``AS``;
        two columns of the same name raise ``SqlError`` (alias one, or use
        ``values``).
        """
        return objects_of(*await self.query_columns(sql, params))

    async def get(self, sql: str, params: Sequence[SqlParam] | None = None) -> Row | None:
        """Return the first row of ``query``, or ``None`` if there is none."""
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    async def values(self, sql: str, params: Sequence[SqlParam] | None = None) -> Rows:
        """Run one statement and return its rows as lists, in column order."""
        return (await self.query_columns(sql, params))[1]

    async def execute(self, sql: str, params: Sequence[SqlParam] | None = None) -> None:
        """Run SQL and discard any rows; ``sql`` may hold several statements, but parameters bind only to one."""
        await self.query_columns(sql, params)

    async def _request(self, call: Callable[[], T | Awaitable[T]]) -> T:
        """Make a request with ``call`` and return its result, translating errors with ``_failure``.

        ``call`` makes the request itself, because the extension may raise as
        soon as it is called (for arguments it cannot convert, or a request
        that fails right away); it returns a value or an awaitable of one.
        """
        try:
            return await _settled(call())
        except Exception as error:
            raise self._failure(error) from None

    def _failure(self, error: Exception) -> BaseException:
        """Return the exception to raise for ``error``.

        That is an ``SqlError`` if a retry would fail the same way, or the
        store's own error if that is what ended the session.
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


# The innermost transaction whose body is running in this context (or in a task
# it started). It refers to the transactions it runs inside.
_running: contextvars.ContextVar[ConnectionTransaction | None] = contextvars.ContextVar(
    "restate_sessions_running_transaction", default=None
)


class Connection(_Sql):
    """A session of a database.

    Calls run one at a time, in the order they were made. A transaction holds
    the session until it commits or rolls back, and calls made meanwhile wait.
    A call made from inside the transaction's own body is refused instead, as
    it would wait forever; the body must use its ``tx``.
    """

    def __init__(self, native: PySqlDatabase, boundary: _Boundary) -> None:
        super().__init__(native, boundary)
        # FIFO: calls run in the order they were made.
        self._lock: asyncio.Lock = asyncio.Lock()

    @asynccontextmanager
    async def _serial(self) -> AsyncGenerator[None, None]:
        """Wait for earlier calls to finish, refusing calls from inside a transaction that holds this session."""
        running = _running.get()
        if running is not None and running.holds(self):
            raise SqlError("inside a transaction, use the transaction handle its body gets")
        async with self._lock:
            yield

    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        async with self._serial():
            return await self._request(lambda: self._native.query_columns(sql, params))

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[ConnectionTransaction, None]:
        """Run the body of an ``async with`` in one SQL transaction::

            async with db.transaction() as tx:
                await tx.execute("UPDATE stock SET n = n - 1 WHERE id = ?", [7])

        The transaction commits, together with the records it set, when the
        body ends. If the body raises, the transaction rolls back once the
        calls it made have completed, and the body's exception propagates. A
        statement that ends the transaction itself (``COMMIT``, ``ROLLBACK``)
        ends the session. If the commit fails, the transaction rolls back.
        """
        async with self._serial():
            await self._request(self._native.begin)
            tx = ConnectionTransaction(self._native, self._boundary, self, _running.get())
            token = _running.set(tx)
            try:
                yield tx
            except BaseException:
                await tx.end()
                await self._rollback()
                raise
            finally:
                _running.reset(token)
            records = await tx.end()
            # The commit's sync runs the callbacks given to `with_commit`. A commit
            # that wrote nothing does not sync, so they run afterwards instead.
            self._boundary.with_sync = tx.commit_runs()
            try:
                await _settled(self._native.commit(records))
            except Exception as error:
                # A failed COMMIT (a deferred constraint, say) leaves the
                # transaction open; without a rollback, later calls would run
                # inside it.
                await self._rollback()
                raise self._failure(error) from None
            finally:
                unsynced, self._boundary.with_sync = self._boundary.with_sync, None
            if unsynced is not None:
                unsynced()

    async def record(self, name: str) -> bytes:
        """Return the committed value of the record ``name``, or empty bytes if it was never set.

        See ``ConnectionTransaction.set_record``.
        """
        async with self._serial():
            return await self._request(lambda: self._native.record(name))

    def is_usable(self) -> bool:
        """Return whether SQL can still run; an SQL error leaves the session usable, a storage error does not."""
        return self._native.is_usable()

    async def save(self) -> None:
        """End the session once earlier calls finish, handing its engine to the next session.

        Fails if those calls left a transaction open, or if a failure already
        ended the session.
        """
        async with self._serial():
            await self._request(self._native.save)

    async def close(self) -> None:
        """End the session immediately, whatever its state; queued calls fail."""
        await _settled(self._native.close())

    async def _rollback(self) -> None:
        """Roll back, ignoring errors; the caller raises its own error, which explains the failure."""
        with contextlib.suppress(Exception):
            await _settled(self._native.rollback())


class ConnectionTransaction(_Sql):
    """SQL inside one transaction, available while its body runs, and the records it commits.

    Calls run one at a time, in call order, even when the body awaits several
    of them together or not at all. Every call made while the transaction is
    open runs before it commits or rolls back, so none outlives it.
    """

    def __init__(
        self, native: PySqlDatabase, boundary: _Boundary, connection: Connection, outer: ConnectionTransaction | None
    ) -> None:
        super().__init__(native, boundary)
        self._connection: Connection = connection
        # The transaction whose body this one runs in, if any. It belongs to
        # another connection, since nesting on the same connection is refused.
        self._outer: ConnectionTransaction | None = outer
        self._records: dict[str, bytes] = {}
        self._with_commits: list[Callable[[], None]] = []
        self._lock: asyncio.Lock = asyncio.Lock()
        self._ended: bool = False

    @property
    def open(self) -> bool:
        """Whether the transaction is still open, holding its connection while its body runs."""
        return not self._ended

    def holds(self, connection: Connection) -> bool:
        """Return whether this transaction, or one it runs inside, is open and holds ``connection``."""
        if not self._ended and self._connection is connection:
            return True
        return self._outer is not None and self._outer.holds(connection)

    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        async with self._serial():
            return await self._request(lambda: self._native.query_columns(sql, params))

    def set_record(self, name: str, value: bytes) -> None:
        """Set the record ``name`` to ``value``, committed atomically with this transaction.

        A record is a whole value stored next to the SQL tables, not in one.
        It is written exactly when the transaction commits, even if no row
        changed, and keeps its value until a later transaction sets it again.
        An empty value reads as never set. Names are at most 255 bytes and may
        not be ``db``; values are at most 16 MiB.
        """
        self._refuse_ended()
        self._records[name] = bytes(value)

    async def record(self, name: str) -> bytes:
        """Return the record ``name`` as this transaction would commit it."""
        # Read what was set at call time, to stay in call order with `set_record`.
        value = self._records.get(name)
        async with self._serial():
            if value is not None:
                return value
            return await self._request(lambda: self._native.record(name))

    def with_commit(self, run: Callable[[], None]) -> None:
        """Call ``run`` during the commit, after its writes and before waiting for them to be stored.

        Whatever ``run`` hands the store's host (Restate sends, which are
        stored in order with the writes) therefore follows the writes, and
        comes before anything that could fail the wait. ``run`` is not called
        if the transaction rolls back.
        """
        self._refuse_ended()
        self._with_commits.append(run)

    async def end(self) -> dict[str, bytes]:
        """Refuse further calls, wait for pending ones, and return the records this transaction set."""
        self._ended = True
        async with self._lock:
            pass
        return dict(self._records)

    def commit_runs(self) -> Callable[[], None] | None:
        """Return a function that runs the ``with_commit`` callbacks, or ``None`` if there are none."""
        runs = self._with_commits
        if not runs:
            return None

        def run_all() -> None:
            for run in runs:
                run()

        return run_all

    @asynccontextmanager
    async def _serial(self) -> AsyncGenerator[None, None]:
        """Wait for earlier calls of this transaction to finish, refusing calls once it has ended."""
        self._refuse_ended()
        async with self._lock:
            yield

    def _refuse_ended(self) -> None:
        if self._ended:
            raise SqlError("the transaction has ended")
