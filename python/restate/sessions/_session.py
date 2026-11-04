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
An attempt's database: the session's database, kept in the object's state
(``_database.py``), opened as an exclusive attempt starts (a view's on its
first read) and saved when the handler ends; and its transactions, with the
sends each commits with (``tx.send``).
"""

from __future__ import annotations

import abc
import asyncio
import base64
import contextlib
import json
import re
import time
import typing
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol
from urllib.parse import quote

from restate._internal import PyVM  # pylint: disable=no-name-in-module
from restate.exceptions import SuspendedException, TerminalError
from restate.sessions._calls import CallHost, GenericSend, Send
from restate.sessions._database import (
    Connection,
    ConnectionTransaction,
    Row,
    Rows,
    SqlError,
    SqlParam,
    Store,
    objects_of,
)
from restate.sessions._database import (
    open as open_connection,
)
from restate.sessions._link import Link
from restate.sessions._schema import Schema, apply, drop_all, ensure_of, ensuring

T = typing.TypeVar("T")


class SqlHost(CallHost, Protocol):
    """What an attempt's session needs of the handler's context."""

    @property
    def shared(self) -> bool:
        """A view's: it reads beside the exclusive handler, and stores nothing."""
        ...

    @property
    def service(self) -> str:
        """The object's name."""
        ...

    @property
    def key(self) -> str:
        """The object's key."""
        ...

    def get(self, key: str) -> Awaitable[bytes | None]:
        """A state read, issued now."""
        ...

    def set(self, key: str, value: bytes) -> None:
        """A state write."""
        ...

    def clear(self, key: str) -> None:
        """A state clear."""
        ...

    def state_keys(self) -> Awaitable[list[str]]:
        """The object's state keys, issued now."""
        ...

    def sync(self) -> Awaitable[Any]:
        """An empty run: done once every command before it is stored."""
        ...

    def attempt_end(self) -> BaseException | None:
        """Why the attempt ended (a suspension, say), or None while it goes on."""
        ...

    def on_attempt_end(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call ``listener`` when the attempt ends; the returned function cancels it."""
        ...

    @property
    def vm(self) -> PyVM | None:
        """The invocation's VM, which the store writes and reads directly; none without one (a test's host)."""
        ...

    def wait(self, handle: int) -> Awaitable[None]:
        """Done once the VM has the read whose handle is ``handle``."""
        ...


@dataclass
class ObjectConfig:
    """What a session object gives its attempts: its schema, and a wrapper of its store (fault injection, in tests)."""

    schema: Schema | None = None
    wrap_store: Callable[[Store], Store] | None = field(default=None)


# --- Errors ---------------------------------------------------------------------------------------


def handler_error(error: BaseException) -> BaseException:
    """
    What a failed call to the session fails the handler with: an ``SqlError``,
    which a retry would repeat, becomes a ``TerminalError``, its cause the SQL
    error. Anything else is as for any handler: a storage error, which ends the
    session, or the handler's own (a failed call to another service, say) is
    retried, unless it is a ``TerminalError`` itself.
    """
    if not isinstance(error, SqlError):
        return error
    terminal = TerminalError(str(error))
    terminal.__cause__ = error
    return terminal


def _raise_for_handler(error: BaseException) -> typing.NoReturn:
    """Raise what ``error`` fails the handler with (see ``handler_error``)."""
    failure = handler_error(error)
    if failure is error:
        raise error
    raise failure


# --- The object's state as a store ----------------------------------------------------------------


def _beside(name: str, key: str) -> str:
    """
    The key of the database ``name``, apart from the object's own: each of the
    format's keys (``v22/head``, ``v22/tail/...``) as ``v22/db/<name>/head``,
    under the format's prefix still, which tooling hides, and under none of the
    object's database's.
    """
    at = key.index("/") + 1
    return f"{key[:at]}db/{name}/{key[at:]}"


def _unbeside(name: str, key: str) -> str | None:
    """The format's key of a key of the database ``name`` (undoes ``_beside``); None for any other key of the object."""
    at = key.find("/") + 1
    prefix = f"db/{name}/"
    if at > 0 and key.startswith(prefix, at):
        return key[:at] + key[at + len(prefix) :]
    return None


class _ObjectStore:
    """
    The object's state as an ordered store: ``get`` is a state get, ``set`` and
    ``clear`` are state writes, which return at once and which the runtime
    stores in order; ``sync`` is an empty run, done once the commands before it
    are stored. A ``get``, ``wait`` or ``sync`` the attempt's end leaves
    waiting raises, so the session that made it ends instead of holding its
    lock. With the invocation's VM (``vm``), the extension writes and reads it
    directly, and waits for a read with ``wait``.
    """

    def __init__(self, host: SqlHost, name: str | None) -> None:
        self._host: SqlHost = host
        self._name: str | None = name
        self.vm: PyVM | None = host.vm
        self.database_name: str | None = name

    def wait(self, handle: int) -> Awaitable[None]:
        """Done once the VM has the read whose handle is ``handle``."""
        return _until_attempt_end(self._host, self._host.wait(handle))

    def _key(self, key: str) -> str:
        return key if self._name is None else _beside(self._name, key)

    def get(self, key: str) -> Awaitable[bytes | None]:
        # TODO: read with protocol v8's ephemeral get, left out of the journal,
        # once a Restate release ships v8 and the SDK can make one. Restate 1.8
        # negotiates v7 at most: a journaled get.
        return _until_attempt_end(self._host, self._host.get(self._key(key)))

    def set(self, key: str, value: bytes) -> None:
        self._host.set(self._key(key), value)

    def clear(self, key: str) -> None:
        self._host.clear(self._key(key))

    def keys(self) -> Awaitable[list[str]]:
        """The database's keys, for a garbage collection: listed now."""
        listing = _until_attempt_end(self._host, self._host.state_keys())
        name = self._name
        if name is None:
            return listing

        async def own() -> list[str]:
            return [own for own in (_unbeside(name, key) for key in await listing) if own is not None]

        return own()

    def sync(self) -> Awaitable[None]:
        """Done once every write before it is stored: the run is made now, in order with them."""
        return _until_attempt_end(self._host, self._host.sync())


class _ReaderStore:
    """The object's state as a view's store: it reads beside the exclusive handler, and writes nothing."""

    def __init__(self, host: SqlHost, name: str | None) -> None:
        self._host: SqlHost = host
        self._name: str | None = name
        self.vm: PyVM | None = host.vm
        self.database_name: str | None = name

    def wait(self, handle: int) -> Awaitable[None]:
        """Done once the VM has the read whose handle is ``handle``."""
        return _until_attempt_end(self._host, self._host.wait(handle))

    def get(self, key: str) -> Awaitable[bytes | None]:
        key = key if self._name is None else _beside(self._name, key)
        return _until_attempt_end(self._host, self._host.get(key))

    def set(self, _key: str, _value: bytes, /) -> None:
        raise RuntimeError("a view writes no state")

    def clear(self, _key: str, /) -> None:
        raise RuntimeError("a view writes no state")

    async def sync(self) -> None:
        pass


async def _until_attempt_end(host: SqlHost, request: Awaitable[T]) -> T:
    """
    ``request``'s result, or why the attempt ended, once it has: an attempt that
    is suspended or aborted never completes what it was waiting for.
    """
    ended = host.attempt_end()
    if ended is not None:
        raise ended
    task = asyncio.ensure_future(request)
    end = asyncio.get_running_loop().create_future()

    def ended_now() -> None:
        if not end.done():
            end.set_result(None)

    cancel = host.on_attempt_end(ended_now)
    try:
        await asyncio.wait({task, end}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        cancel()
    if task.done():
        return task.result()
    # Its error, if it ever has one, is dropped.
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    raise host.attempt_end() or SuspendedException()


# --- The outbox -----------------------------------------------------------------------------------

# A transaction's sends commit with it, in the database's record `outbox`
# (beside its tables, in none of them), which holds each committed
# transaction's sends until they are stored. They are made with its commit
# (see `Attempt.transaction`); a later open makes again those still in the
# outbox, with the same idempotency keys (`transaction/<id>/<index>`, with a
# random id of the transaction), so making them again is dropped. Restate
# keeps an attempt's commands in the order they were made, so a send made
# before a commit is stored whenever the commit is: the next commit leaves the
# sends this attempt made out of the outbox, and so does the end of the
# attempt. The format is sdk-sessions' (TypeScript).

OUTBOX = "outbox"
"""The database's record of the outbox."""

Outgoing = dict[str, Any]
"""The sends of a committed transaction, in the outbox: ``{"id": ..., "sends": [...]}``."""


def _encode_outbox(outbox: list[Outgoing]) -> bytes:
    """The outbox as its record: none is empty, as a record never set reads."""
    return json.dumps(outbox).encode() if outbox else b""


def _decode_outbox(record: bytes) -> list[Outgoing]:
    return json.loads(record.decode()) if record else []


def _now_ms() -> int:
    return int(time.time() * 1000)


class _Sends:
    """The sends of one transaction, as its body makes them (``tx.send``), until it ends."""

    def __init__(self) -> None:
        self._sends: list[dict[str, Any]] = []
        self._open: bool = True
        self.send: Send = Send(self._record)

    def _record(self, send: GenericSend) -> None:
        if not self._open:
            raise TerminalError("the transaction has ended: send from inside its body")
        stored: dict[str, Any] = {
            "service": send.service,
            "method": send.handler,
            "parameter": base64.b64encode(send.parameter).decode("ascii"),
        }
        if send.key is not None:
            stored["key"] = send.key
        if send.delay is not None:
            stored["delay"] = int(send.delay.total_seconds() * 1000)
        if send.idempotency_key is not None:
            stored["idempotencyKey"] = send.idempotency_key
        if send.headers:
            stored["headers"] = send.headers
        self._sends.append(stored)

    def close(self) -> Outgoing | None:
        """The body ended: the transaction's sends, if it made any, their delays counted from now."""
        self._open = False
        if not self._sends:
            return None
        now = _now_ms()
        for send in self._sends:
            if "delay" in send:
                send["due"] = now + send["delay"]
        return {"id": str(uuid.uuid4()), "sends": self._sends}


def _refused_send(_: GenericSend) -> None:
    """The sends of a transaction that has no outbox: a view's, or another database's."""
    raise TerminalError("a send of a transaction is an exclusive handler's, in the object's own database")


# --- The database, as a handler gets it -----------------------------------------------------------


class DatabaseReader(abc.ABC):
    """Reads of a database, or of one transaction of it."""

    @abc.abstractmethod
    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        """The rows of one statement as lists, with the column names of the statement: ``(columns, rows)``."""

    async def query(self, sql: str, params: Sequence[SqlParam] | None = None) -> list[Row]:
        """
        The rows of one statement, each a dict by column name; parameters bind
        to a single statement. Columns are named as SQL names them (an
        expression needs ``AS``), and two of the same name are an error: alias
        them, or use ``values``.
        """
        columns, rows = await self.query_columns(sql, params)
        try:
            return objects_of(columns, rows)
        except SqlError as error:
            _raise_for_handler(error)

    async def get(self, sql: str, params: Sequence[SqlParam] | None = None) -> Row | None:
        """The first row of ``query``, or ``None`` when there is none."""
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    async def values(self, sql: str, params: Sequence[SqlParam] | None = None) -> Rows:
        """The rows of one statement as lists, in column order: for adapters, and for columns that share a name."""
        return (await self.query_columns(sql, params))[1]


class DatabaseWriter(DatabaseReader, abc.ABC):
    """SQL: a database, or one transaction of it."""

    async def execute(self, sql: str, params: Sequence[SqlParam] | None = None) -> None:
        """Run SQL (one or more statements), discarding rows; parameters only with a single statement."""
        await self.query_columns(sql, params)


class Transaction(DatabaseWriter):
    """
    One transaction of a ``Database``: its SQL, and the sends it commits with.

    ``send`` makes its sends right after the transaction commits, and never if
    it rolls back. Each has the idempotency key it is given, or else one of the
    transaction (``transaction/<id>/<index>``), so a send an attempt makes
    again is dropped.
    """

    def __init__(self, handle: ConnectionTransaction, send: Send) -> None:
        self._handle: ConnectionTransaction = handle
        self.send: Send = send

    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        try:
            return await self._handle.query_columns(sql, params)
        except SqlError as error:
            _raise_for_handler(error)


class Database(DatabaseWriter):
    """
    The session's database, ``ctx.db``, for the whole handler: each statement
    outside ``transaction()`` is its own transaction. Calls run one at a time,
    in call order. A commit is durable once its call returns.
    """

    def __init__(self, attempt: Attempt) -> None:
        self._attempt: Attempt = attempt
        _ATTEMPT.on(self, attempt)

    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        return await self._attempt.call(lambda db: db.query_columns(sql, params))

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
        return self._attempt.transaction()


class _ReaderOf(DatabaseReader):
    """The reads of a ``Database``: a view's ``ctx.db``."""

    def __init__(self, db: Database) -> None:
        self._db: Database = db

    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        return await self._db.query_columns(sql, params)


def reader_of(db: Database) -> DatabaseReader:
    """The reads of ``db``, which apply a library's schema as ``db`` does (``Schema.ensure``)."""
    reader = _ReaderOf(db)
    ensure = ensure_of(db)
    return ensuring(reader, ensure) if ensure is not None else reader


_DATABASE_NAME = re.compile(r"^[\w.-]{1,64}$")

_ATTEMPT: Link[Attempt] = Link("database")
"""The attempt of a ``ctx.db``: for ``database``."""

_RESET: Link[Callable[[], Awaitable[None]]] = Link("reset")
"""How a transaction of an exclusive handler's ``ctx.db`` empties the database: for ``reset``."""


def database(db: Database, name: str) -> Database:
    """
    The database ``name`` of the key, apart from the object's own (``ctx.db``,
    of the attempt whose database ``db`` is): for a library whose tables must
    be alone in their database. It opens on its first use, and ends with the
    attempt, as ``ctx.db`` does. A name is 1 to 64 letters, digits, ``_``,
    ``-`` or ``.``.
    """
    if not _DATABASE_NAME.match(name):
        raise TerminalError(f"database: {name!r} is not a database name")
    attempt = _ATTEMPT.of(db)
    if attempt is None:
        raise TerminalError("database takes a handler's ctx.db")
    return attempt.database(name)


async def reset(tx: Transaction) -> None:
    """
    Empty the key's database in ``tx``, a transaction of an exclusive handler's
    ``ctx.db``: every table, view, index and trigger is dropped, the schemas are
    applied again (the object's, and the libraries' the attempt applied), and
    the outbox stays. It commits with ``tx``: what ``tx`` writes after it are
    the new database's first rows.
    """
    empty = _RESET.of(tx)
    if empty is None:
        raise TerminalError("reset takes a transaction of an exclusive handler's ctx.db")
    try:
        await empty()
    except SqlError as error:
        _raise_for_handler(error)


# --- An attempt -----------------------------------------------------------------------------------


def _close_soon(db: Connection) -> None:
    task = asyncio.ensure_future(db.close())
    task.add_done_callback(lambda t: t.cancelled() or t.exception())


class _Session:
    """
    One session of an attempt's database, from its open on. A failure ends it:
    its open's, or a request's since. It closes as the attempt ends, unless the
    attempt ends it first (``end``).
    """

    def __init__(self, ready: asyncio.Task[Connection], host: SqlHost) -> None:
        self.ready: asyncio.Task[Connection] = ready
        # A call failed on it once a failure ended it: the next call opens another.
        self.retired: bool = False
        self._release: Callable[[], None] = lambda: None
        ready.add_done_callback(lambda task: self._track(task, host))

    def _track(self, task: asyncio.Task[Connection], host: SqlHost) -> None:
        if task.cancelled() or task.exception() is not None:
            return
        db = task.result()
        # A handler still waiting for something else when the attempt ends (a
        # model's request, say) keeps the session no longer: it closes once no
        # statement runs, and the handler's later calls fail.
        self._release = host.on_attempt_end(lambda: _close_soon(db))

    @property
    def ended(self) -> bool:
        """Whether a failure ended it."""
        if not self.ready.done():
            return False
        if self.ready.cancelled() or self.ready.exception() is not None:
            return True
        return not self.ready.result().is_usable()

    async def end(self) -> Connection | None:
        """Its connection, or none if the open failed; the attempt's end no longer closes it."""
        try:
            db: Connection | None = await asyncio.shield(self.ready)
        except asyncio.CancelledError:
            # Ours, as the task awaiting is cancelled, unless the open itself was.
            if not self.ready.cancelled():
                raise
            db = None
        except BaseException:  # pylint: disable=broad-exception-caught
            db = None
        self._release()
        return db

    def close(self) -> None:
        """Close it now."""

        async def close() -> None:
            db = await self.end()
            if db is not None:
                await db.close()

        task = asyncio.ensure_future(close())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())


class Attempt:
    """
    An attempt's SQL session: opened on its first use, or as an exclusive
    attempt starts (``open_now``), shared by everything the handler does, and
    saved when the handler ends (``end``). With ``name``, of the key's database
    ``name`` (``database``), which has neither the object's schema nor an
    outbox.
    """

    def __init__(self, host: SqlHost, config: ObjectConfig | None = None, name: str | None = None) -> None:
        self._host: SqlHost = host
        self._config: ObjectConfig = config or ObjectConfig()
        self._name: str | None = name
        # The object's own database of an exclusive handler: the one with an outbox.
        self._has_outbox: bool = not host.shared and name is None
        # The key's other databases this attempt opened, by name.
        self._databases: dict[str, Attempt] = {}
        self._session: _Session | None = None
        # The ids of the outbox's transactions this attempt made the sends of.
        self._made: set[str] = set()
        # The schemas this attempt's database has all the steps of, by name.
        self._ensured: dict[str, Schema] = {}
        self._ended: bool = False
        self.db: Database = Database(self)
        """``ctx.db``."""
        ensuring(self.db, self._ensure)

    async def _ensure(self, schemas: Sequence[Schema]) -> None:
        await self.call(lambda db: self._ensure_schemas(db, schemas))

    def database(self, name: str) -> Database:
        """The key's database ``name``, in this attempt: see ``database``."""
        if self._ended:
            raise RuntimeError("the handler has ended: its SQL session is closed")
        other = self._databases.get(name)
        if other is None:
            other = self._databases[name] = Attempt(self._host, self._config, name)
        return other.db

    def open_now(self) -> None:
        """
        Open the session now, as the attempt starts, rather than on its first
        call: the open makes the sends a crashed attempt committed and left
        unmade, which must not wait for a handler that uses the database.
        """
        if self._has_outbox and self._session is None:
            # Its failure goes to the first call, as it would had that call opened it.
            self._current().ready.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def end(self, error: BaseException | None) -> None:
        """
        The handler ended, with ``error`` or not: the key's other databases end,
        then this one. It raises what the handler fails with (see
        ``handler_error``), or a failure to end the session.
        """
        failures: list[BaseException] = []
        for other in self._databases.values():
            try:
                await other.end(None)
            except BaseException as failure:  # pylint: disable=broad-exception-caught
                failures.append(failure)
        await self._end_session(error)
        if failures:
            raise failures[0]

    async def call(self, call: Callable[[Connection], Awaitable[T]]) -> T:
        """
        ``call`` on the session, opening it on the first; its failure as the
        handler fails with it. The call that fails on a session that a failure
        ended is its last: the next opens another, which reads what is stored.
        """
        session = self._current()
        try:
            return await call(await session.ready)
        except BaseException as error:
            if session.ended:
                session.retired = True
            _raise_for_handler(error)

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[Transaction, None]:
        """
        One transaction. In the object's own database of an exclusive handler,
        its sends commit with it, in the outbox, and are made with its commit,
        right after its writes and before it waits for anything
        (``with_commit``): Restate stores an attempt's commands in order, so
        they are stored only if the commit is, and whatever fails the commit's
        wait (a cancellation, say) comes after them. A second commit empties
        the outbox before the transaction returns: left until the handler's
        end, they would be made again by an attempt that resumes a long wait,
        past the idempotency retention that drops a repeat.
        """
        outgoing: Outgoing | None = None
        session = self._current()
        try:
            db = await session.ready
            async with db.transaction() as handle:
                sends = _Sends() if self._has_outbox else None
                # A view's writes stay in its session: it has nothing to reset.
                tx = Transaction(handle, sends.send if sends is not None else Send(_refused_send))
                if not self._host.shared:
                    _RESET.on(tx, lambda: self._reset(handle))
                try:
                    yield tx
                finally:
                    outgoing = sends.close() if sends is not None else None
                if self._has_outbox:
                    await self._commit_outbox(handle, outgoing)
        except BaseException as error:
            if session.ended:
                session.retired = True
            _raise_for_handler(error)
        if outgoing is not None:
            await self.call(self._flush)

    async def _commit_outbox(self, handle: ConnectionTransaction, outgoing: Outgoing | None) -> None:
        """Commit with ``handle`` the outbox as committed, but the sends stored since, and with ``outgoing``, which the commit makes."""
        outbox = await self._unstored(handle)
        if outgoing is not None:
            outbox.append(outgoing)
            sent = outgoing
            handle.with_commit(lambda: self._make(sent))
        handle.set_record(OUTBOX, _encode_outbox(outbox))

    def _current(self) -> _Session:
        """The session, opening one if there is none, or the one there is was retired."""
        if self._ended:
            raise RuntimeError("the handler has ended: its SQL session is closed")
        if self._session is not None and self._session.retired:
            self._session.close()
        if self._session is None or self._session.retired:
            self._session = self._open()
        return self._session

    def _open(self) -> _Session:
        return _Session(asyncio.ensure_future(self._start()), self._host)

    def _own_schema(self) -> Schema | None:
        """The object's schema, which the key's other databases have none of."""
        return self._config.schema if self._name is None else None

    async def _start(self) -> Connection:
        host = self._host
        # The page cache's: the key escaped, so that no key reads as another's
        # database (a name has no `/`).
        key = f"{host.service}/{quote(host.key, safe='')}"
        namespace = key if self._name is None else f"{key}/{self._name}"
        store: Store = _ReaderStore(host, self._name) if host.shared else _ObjectStore(host, self._name)
        if self._config.wrap_store is not None:
            store = self._config.wrap_store(store)
        db = await open_connection(namespace, store, reader=host.shared)
        try:
            # A view writes nothing: a write fails, rather than vanish with the attempt.
            if host.shared:
                await db.execute("PRAGMA query_only = 1")
            if self._has_outbox:
                # What a crash left unmade; an open again in this attempt finds
                # what this attempt made, stored before it, as made.
                left = _decode_outbox(await db.record(OUTBOX))
                for outgoing in left:
                    if outgoing["id"] not in self._made:
                        self._make(outgoing, again=True)
                # Emptied at once, as a transaction's own sends are.
                if left:
                    await self._flush(db)
            # A view's too, whose writes are never stored: a key never written reads as empty tables.
            own = self._own_schema()
            if own is not None:
                await self._ensure_schemas(db, [own])
        except BaseException:
            await db.close()
            raise
        return db

    async def _end_session(self, error: BaseException | None) -> None:
        """
        End the session, then raise as the handler did: an SQL error that
        escaped it becomes terminal. The handler's error stands: what it
        committed is stored, and an outbox left full is made by the next open.
        A failure to end the session fails a handler that succeeded, which a
        retry then ends (a transaction it left open, say).
        """
        self._ended = True
        failed: BaseException | None = None
        try:
            await self._close()
        except BaseException as failure:  # pylint: disable=broad-exception-caught
            failed = failure
        if error is not None:
            _raise_for_handler(error)
        if failed is not None:
            _raise_for_handler(failed)

    async def _close(self) -> None:
        """
        Close the session as the attempt ends. A view's closes: it read the
        snapshot it opened on. Another is saved, its outbox emptied. One that a
        failure ended closes, and the object's own database is opened again and
        saved: it may hold sends unmade (a crash left them, and this attempt's
        open failed) or made and not emptied (the commit's wait failed).
        """
        if self._session is None:
            return
        db = await self._session.end()
        if self._host.shared:
            if db is not None:
                await db.close()
            return
        if db is not None and db.is_usable():
            await self._save(db)
            return
        if db is not None:
            with contextlib.suppress(Exception):
                await db.close()
        if not self._has_outbox or self._host.attempt_end() is not None:
            return
        again = self._open()
        try:
            await self._save(await again.ready)
        finally:
            await again.end()

    async def _save(self, db: Connection) -> None:
        """Empty the outbox and save the session, which closes if that fails."""
        try:
            if self._has_outbox:
                await self._flush(db)
            await db.save()
        except BaseException:
            with contextlib.suppress(Exception):
                await db.close()
            raise

    async def _reset(self, handle: ConnectionTransaction) -> None:
        """Empty the database in the transaction ``handle`` (``reset``), and apply the schemas this attempt applied again."""
        own = self._own_schema()
        schemas = ([own] if own is not None else []) + [s for s in self._ensured.values() if s is not own]
        await drop_all(handle, schemas)

    async def _ensure_schemas(self, db: Connection, schemas: Sequence[Schema]) -> None:
        """Apply the steps of ``schemas`` that the database lacks, each schema once per attempt."""
        lacking = [s for s in schemas if s.name not in self._ensured]
        if not lacking:
            return
        await apply(db, lacking, reader=self._host.shared)
        for schema in lacking:
            self._ensured[schema.name] = schema

    def _make(self, outgoing: Outgoing, again: bool = False) -> None:
        """Make the sends of a committed transaction: each with an idempotency key of the transaction (or its own), so that making them again is dropped."""
        for index, send in enumerate(outgoing["sends"]):
            delay = send.get("delay")
            due = send.get("due")
            if delay is not None and again and due is not None:
                # Made again, a delayed send waits only what remains of its delay.
                delay = max(0, due - _now_ms())
            self._host.generic_send(
                send["service"],
                send["method"],
                base64.b64decode(send["parameter"]),
                send.get("key"),
                None if delay is None else timedelta(milliseconds=delay),
                send.get("idempotencyKey") or f"transaction/{outgoing['id']}/{index}",
                send.get("headers"),
            )
        self._made.add(outgoing["id"])

    async def _unstored(self, db: Connection | ConnectionTransaction) -> list[Outgoing]:
        """The outbox as committed, but the transactions this attempt made the sends of: a commit made after them stores them first."""
        return [outgoing for outgoing in _decode_outbox(await db.record(OUTBOX)) if outgoing["id"] not in self._made]

    async def _flush(self, db: Connection) -> None:
        """Empty the outbox, so that a later open does not make its sends again: a commit after them, which stores them first."""
        if not await db.record(OUTBOX):
            return
        # Read in the transaction that writes it: one that committed sends since keeps them.
        async with db.transaction() as tx:
            tx.set_record(OUTBOX, _encode_outbox(await self._unstored(tx)))


async def with_attempt(handler: Callable[[], Awaitable[T]], attempt: Attempt) -> T:
    """Run ``handler``, then end ``attempt``'s SQL session, if it opened one (see ``Attempt.end``)."""
    try:
        value = await handler()
    except BaseException as error:
        await attempt.end(error)
        raise  # `end` raises: this is for the type checker
    await attempt.end(None)
    return value
