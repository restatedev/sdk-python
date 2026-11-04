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
"""The database of one attempt, and its transactions.

The database itself is stored in the object's state (see ``_database.py``).
An exclusive handler's attempt opens it when it starts, a view's on its
first read, and the attempt saves it when the handler ends. Transactions
commit together with the sends made through ``tx.send``.
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
from restate.sessions._api import Database, DatabaseReader, Transaction
from restate.sessions._calls import CallHost, GenericSend, Sends
from restate.sessions._database import (
    Connection,
    ConnectionTransaction,
    Row,
    Rows,
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
    """The host that an attempt's database runs on: the handler's context, or a test's fake.

    Implement it to run handlers without Restate, with ``internal.invoke``.
    """

    @property
    def shared(self) -> bool:
        """Whether this is a view's attempt, which reads alongside the exclusive handler and stores nothing."""
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
        """Read the state ``key``; the read is issued immediately."""
        ...

    def set(self, key: str, value: bytes) -> None:
        """Write the state ``key``."""
        ...

    def clear(self, key: str) -> None:
        """Clear the state ``key``."""
        ...

    def state_keys(self) -> Awaitable[list[str]]:
        """List the object's state keys; the request is issued immediately."""
        ...

    def sync(self) -> Awaitable[Any]:
        """Make an empty run, which completes once every earlier command is stored."""
        ...

    def attempt_end(self) -> BaseException | None:
        """Return why the attempt ended (a suspension, say), or ``None`` while it continues."""
        ...

    def on_attempt_end(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call ``listener`` when the attempt ends, and return a function that unregisters it."""
        ...

    @property
    def vm(self) -> PyVM | None:
        """The invocation's VM, which the database reads and writes directly, or ``None`` (as on a test's host)."""
        ...

    def wait(self, handle: int) -> Awaitable[None]:
        """Wait until the VM has the result of the read ``handle``."""
        ...

    def cancel_invocation(self, invocation_id: str) -> None:
        """Cancel the invocation ``invocation_id``."""
        ...


@dataclass
class ObjectConfig:
    """Per-object settings shared by its attempts.

    Attributes:
        schema: The object's migrations, as a ``Schema``.
        wrap_store: A function that wraps the store, for fault injection in tests.
    """

    schema: Schema | None = None
    wrap_store: Callable[[Store], Store] | None = field(default=None)


# --- The object's state as a store ----------------------------------------------------------------


def _beside(name: str, key: str) -> str:
    """Return the state key of ``key`` for the separate database ``name``.

    ``v22/head`` becomes ``v22/db/<name>/head``: still under the format's
    prefix, which tooling hides, and clear of the object's own database's keys.
    """
    at = key.index("/") + 1
    return f"{key[:at]}db/{name}/{key[at:]}"


def _unbeside(name: str, key: str) -> str | None:
    """Undo ``_beside``: return the format's key, or ``None`` if ``key`` is not in the database ``name``."""
    at = key.find("/") + 1
    prefix = f"db/{name}/"
    if at > 0 and key.startswith(prefix, at):
        return key[:at] + key[at + len(prefix) :]
    return None


class _ObjectStore:
    """The object's state as a ``Store``.

    ``get`` reads the state; ``set`` and ``clear`` write it, returning at once,
    and Restate stores the writes in order; ``sync`` is an empty run, which
    completes once the commands before it are stored. A ``get``, ``wait`` or
    ``sync`` still pending when the attempt ends raises, so that the session
    ends instead of holding its lock. With the invocation's VM (``vm``), the
    extension reads and writes the VM directly, and waits for reads with
    ``wait``.
    """

    def __init__(self, host: SqlHost, name: str | None) -> None:
        self._host: SqlHost = host
        self._name: str | None = name
        self.vm: PyVM | None = host.vm
        self.database_name: str | None = name

    def wait(self, handle: int) -> Awaitable[None]:
        """Wait until the VM has the result of the read ``handle``."""
        return _until_attempt_end(self._host, self._host.wait(handle))

    def _key(self, key: str) -> str:
        return key if self._name is None else _beside(self._name, key)

    def get(self, key: str) -> Awaitable[bytes | None]:
        # TODO: use protocol v8's ephemeral get, which stays out of the journal,
        # once a Restate release ships v8 and the SDK supports it. Restate 1.8
        # negotiates v7 at most, so this is a journaled get.
        return _until_attempt_end(self._host, self._host.get(self._key(key)))

    def set(self, key: str, value: bytes) -> None:
        self._host.set(self._key(key), value)

    def clear(self, key: str) -> None:
        self._host.clear(self._key(key))

    def keys(self) -> Awaitable[list[str]]:
        """List the database's state keys, for garbage collection; the request is issued immediately."""
        listing = _until_attempt_end(self._host, self._host.state_keys())
        name = self._name
        if name is None:
            return listing

        async def own() -> list[str]:
            return [own for own in (_unbeside(name, key) for key in await listing) if own is not None]

        return own()

    def sync(self) -> Awaitable[None]:
        """Wait until every earlier write is stored; the run is issued now, in order with the writes."""
        return _until_attempt_end(self._host, self._host.sync())


class _ReaderStore:
    """The object's state as a view's ``Store``, which reads alongside the exclusive handler and never writes."""

    def __init__(self, host: SqlHost, name: str | None) -> None:
        self._host: SqlHost = host
        self._name: str | None = name
        self.vm: PyVM | None = host.vm
        self.database_name: str | None = name

    def wait(self, handle: int) -> Awaitable[None]:
        """Wait until the VM has the result of the read ``handle``."""
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
    """Return the result of ``request``, or raise why the attempt ended if it ends first.

    A suspended or aborted attempt never receives what it was waiting for.
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
    # Drop the request's error, if it ever has one.
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    raise host.attempt_end() or SuspendedException()


# --- The outbox -----------------------------------------------------------------------------------

# A transaction's sends commit with it, in the database's `outbox` record
# (stored next to its tables, not in one), which holds each committed
# transaction's sends until they are stored by Restate. They are sent during
# the commit (see `Attempt.transaction`). A later open sends again whatever is
# still in the outbox, with the same idempotency keys
# (`transaction/<id>/<index>`, where the id is random per transaction), so
# Restate drops the duplicates. Restate keeps an attempt's commands in the
# order they were made, so a send made before a commit is stored whenever the
# commit is: the next commit removes the sends this attempt made from the
# outbox, and so does the end of the attempt. The record's format is a wire
# format shared with other SDKs: keep it compatible.

OUTBOX = "outbox"
"""The name of the record that holds the outbox."""

Outgoing = dict[str, Any]
"""The sends of one committed transaction, as stored in the outbox: ``{"id": ..., "sends": [...]}``."""


def _encode_outbox(outbox: list[Outgoing]) -> bytes:
    """Encode the outbox as its record; an empty outbox is stored as empty bytes, like a record never set."""
    return json.dumps(outbox).encode() if outbox else b""


def _decode_outbox(record: bytes) -> list[Outgoing]:
    return json.loads(record.decode()) if record else []


def _now_ms() -> int:
    return int(time.time() * 1000)


class _Sends:
    """The sends that one transaction's body makes with ``tx.send``, collected until it ends."""

    def __init__(self) -> None:
        self._sends: list[dict[str, Any]] = []
        self._open: bool = True

    def record(self, send: GenericSend) -> None:
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
        """Close the collection and return the transaction's sends, if any, with delays counted from now."""
        self._open = False
        if not self._sends:
            return None
        now = _now_ms()
        for send in self._sends:
            if "delay" in send:
                send["due"] = now + send["delay"]
        return {"id": str(uuid.uuid4()), "sends": self._sends}


def _refused_send(_: GenericSend) -> None:
    """Refuse a send from a transaction without an outbox (a view's, or another database's)."""
    raise TerminalError("a send of a transaction is an exclusive handler's, in the object's own database")


# --- The database, as a handler gets it -----------------------------------------------------------


class Reads(abc.ABC):
    """A ``DatabaseReader`` (see ``_api.py``) built on ``query_columns``."""

    @abc.abstractmethod
    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        """Run one statement and return its column names and its rows as lists: ``(columns, rows)``."""

    async def query(self, sql: str, params: Sequence[SqlParam] | None = None) -> list[Row]:
        """Run one statement and return its rows, each a dict keyed by column name.

        Columns are named as SQL names them, so an expression needs ``AS``;
        two columns of the same name raise ``SqlError`` (alias one, or use
        ``values``).
        """
        columns, rows = await self.query_columns(sql, params)
        return objects_of(columns, rows)

    async def get(self, sql: str, params: Sequence[SqlParam] | None = None) -> Row | None:
        """Return the first row of ``query``, or ``None`` if there is none."""
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    async def values(self, sql: str, params: Sequence[SqlParam] | None = None) -> Rows:
        """Run one statement and return its rows as lists, in column order."""
        return (await self.query_columns(sql, params))[1]


class Writes(Reads, abc.ABC):
    """A ``DatabaseWriter`` (see ``_api.py``) built on ``query_columns``."""

    async def execute(self, sql: str, params: Sequence[SqlParam] | None = None) -> None:
        """Run SQL and discard any rows; ``sql`` may hold several statements, but parameters bind only to one."""
        await self.query_columns(sql, params)


class AttemptTransaction(Writes, Sends):
    """The ``Transaction`` (see ``_api.py``) of an attempt: SQL in one transaction, and the sends it commits."""

    def __init__(self, handle: ConnectionTransaction, sink: Callable[[GenericSend], None]) -> None:
        self._handle: ConnectionTransaction = handle
        self._send_sink = sink

    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        return await self._handle.query_columns(sql, params)


class AttemptDatabase(Writes):
    """The ``Database`` (see ``_api.py``) of an attempt, opened on first use."""

    def __init__(self, attempt: Attempt) -> None:
        self._attempt: Attempt = attempt
        _ATTEMPT.on(self, attempt)

    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        return await self._attempt.call(lambda db: db.query_columns(sql, params))

    def transaction(self) -> typing.AsyncContextManager[Transaction]:
        return self._attempt.transaction()


class _ReaderOf(Reads):
    """A read-only view of an ``AttemptDatabase``: a view's ``ctx.db``."""

    def __init__(self, db: AttemptDatabase) -> None:
        self._db: AttemptDatabase = db

    async def query_columns(self, sql: str, params: Sequence[SqlParam] | None = None) -> tuple[list[str], Rows]:
        return await self._db.query_columns(sql, params)


def reader_of(db: AttemptDatabase) -> DatabaseReader:
    """Return a read-only wrapper of ``db`` that still applies schemas (see ``Schema.ensure``)."""
    reader = _ReaderOf(db)
    ensure = ensure_of(db)
    return ensuring(reader, ensure) if ensure is not None else reader


_DATABASE_NAME = re.compile(r"^[\w.-]{1,64}$")

_ATTEMPT: Link[Attempt] = Link("database", weak=True)
"""The attempt that owns a ``ctx.db``, held weakly as it refers back to it; used by ``database``."""

_RESET: Link[Callable[[], Awaitable[None]]] = Link("reset")
"""How a transaction of an exclusive handler's ``ctx.db`` empties the database; used by ``reset``."""


def database(db: Database, name: str) -> Database:
    """Return another database of the same key, separate from the object's own ``ctx.db``.

    This is for libraries whose tables must live in a database of their own.
    Like ``ctx.db``, it opens on first use and closes when the attempt ends.
    It has neither the object's migrations nor an outbox, so ``tx.send`` is
    not available in its transactions::

        checkpoints = internal.database(ctx.db, "checkpoints")

    Args:
        db: A handler's ``ctx.db``.
        name: The database's name: 1 to 64 letters, digits, ``_``, ``-`` or ``.``.

    Returns:
        The database ``name`` of the key.

    Raises:
        TerminalError: If ``name`` is not a valid name, or ``db`` is not a
            handler's ``ctx.db``.
    """
    if not _DATABASE_NAME.match(name):
        raise TerminalError(f"database: {name!r} is not a database name")
    attempt = _ATTEMPT.of(db)
    if attempt is None:
        raise TerminalError("database takes a handler's ctx.db")
    return attempt.database(name)


async def reset(tx: Transaction) -> None:
    """Empty the key's database inside the transaction ``tx``.

    Every table, view, index and trigger is dropped, and then the schemas are
    applied again: the object's migrations, and those of any library this
    attempt used. The outbox is kept. The reset commits with ``tx``, so
    whatever ``tx`` writes afterwards becomes the new database's first rows::

        async with ctx.db.transaction() as tx:
            await internal.reset(tx)

    Args:
        tx: A transaction of an exclusive handler's ``ctx.db``.

    Raises:
        TerminalError: If ``tx`` is not a transaction of an exclusive
            handler's ``ctx.db``.
    """
    empty = _RESET.of(tx)
    if empty is None:
        raise TerminalError("reset takes a transaction of an exclusive handler's ctx.db")
    await empty()


# --- An attempt -----------------------------------------------------------------------------------


def _close_soon(db: Connection) -> None:
    task = asyncio.ensure_future(db.close())
    task.add_done_callback(lambda t: t.cancelled() or t.exception())


class _Session:
    """One session of an attempt's database, from the moment it starts opening.

    A failure, while opening or afterwards, ends it. It closes when the
    attempt ends, unless the attempt ends it first with ``end``.
    """

    def __init__(self, ready: asyncio.Task[Connection], host: SqlHost) -> None:
        self.ready: asyncio.Task[Connection] = ready
        # Set when a call fails on a session that a failure ended; the next call opens a new one.
        self.retired: bool = False
        self._release: Callable[[], None] = lambda: None
        ready.add_done_callback(lambda task: self._track(task, host))

    def _track(self, task: asyncio.Task[Connection], host: SqlHost) -> None:
        if task.cancelled() or task.exception() is not None:
            return
        db = task.result()
        # A handler still waiting for something else when the attempt ends (a
        # model request, say) no longer holds the session: it closes once no
        # statement is running, and the handler's later calls fail.
        self._release = host.on_attempt_end(lambda: _close_soon(db))

    @property
    def ended(self) -> bool:
        """Whether a failure ended the session."""
        if not self.ready.done():
            return False
        if self.ready.cancelled() or self.ready.exception() is not None:
            return True
        return not self.ready.result().is_usable()

    async def end(self) -> Connection | None:
        """Detach the session from the attempt's end and return its connection, or ``None`` if opening failed."""
        try:
            db: Connection | None = await asyncio.shield(self.ready)
        except asyncio.CancelledError:
            # The awaiting task was cancelled, unless the open itself was.
            if not self.ready.cancelled():
                raise
            db = None
        except BaseException:  # pylint: disable=broad-exception-caught
            db = None
        self._release()
        return db

    def close(self) -> None:
        """Close the session now."""

        async def close() -> None:
            db = await self.end()
            if db is not None:
                await db.close()

        task = asyncio.ensure_future(close())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())


class Attempt:
    """The SQL session of one attempt.

    It opens on first use, or when an exclusive attempt starts (``open_now``),
    is shared by everything the handler does, and is saved when the handler
    ends (``end``). With ``name``, it is the key's separate database ``name``
    (see ``database``), which has neither the object's schema nor an outbox.
    """

    def __init__(self, host: SqlHost, config: ObjectConfig | None = None, name: str | None = None) -> None:
        self._host: SqlHost = host
        self._config: ObjectConfig = config or ObjectConfig()
        self._name: str | None = name
        # Only the object's own database, in an exclusive handler, has an outbox.
        self._has_outbox: bool = not host.shared and name is None
        # The key's other databases this attempt opened, by name.
        self._databases: dict[str, Attempt] = {}
        self._session: _Session | None = None
        # The ids of the outbox transactions whose sends this attempt has made.
        self._made: set[str] = set()
        # The schemas fully applied to this attempt's database, by name.
        self._ensured: dict[str, Schema] = {}
        self._ended: bool = False
        self.db: AttemptDatabase = AttemptDatabase(self)
        """``ctx.db``."""
        ensuring(self.db, self._ensure)

    async def _ensure(self, schemas: Sequence[Schema]) -> None:
        await self.call(lambda db: self._ensure_schemas(db, schemas))

    def database(self, name: str) -> Database:
        """Return the key's separate database ``name`` for this attempt (see ``database``)."""
        if self._ended:
            raise RuntimeError("the handler has ended: its SQL session is closed")
        other = self._databases.get(name)
        if other is None:
            other = self._databases[name] = Attempt(self._host, self._config, name)
        return other.db

    def open_now(self) -> None:
        """Open the session now, as the attempt starts, rather than on first use.

        Opening makes any sends that a crashed attempt committed but did not
        make, and those should not wait until the handler uses the database.
        """
        if self._has_outbox and self._session is None:
            # A failure is reported to the first call, as if that call had opened the session.
            self._current().ready.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def end(self, error: BaseException | None) -> None:
        """End the attempt's databases after the handler ends, the key's other databases first.

        Re-raises ``error``, the handler's exception, if there is one;
        otherwise raises any failure to end a session.
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
        """Run ``call`` on the session, opening it if needed, and return its result.

        Errors propagate to the handler. If a call fails on a session that a
        failure ended, the next call opens a new session, which reads what was
        stored.
        """
        session = self._current()
        try:
            return await call(await session.ready)
        except BaseException:
            if session.ended:
                session.retired = True
            raise

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[AttemptTransaction, None]:
        """Run the body of an ``async with`` in one transaction.

        In the object's own database of an exclusive handler, the transaction's
        sends are committed to the outbox with it, and are made during the
        commit, right after its writes and before it waits for anything (see
        ``with_commit``). Since Restate stores an attempt's commands in order,
        the sends are stored only if the commit is, and anything that fails the
        commit's wait (a cancellation, say) comes after them. A second commit
        empties the outbox before the transaction returns. If the sends stayed
        until the handler's end, an attempt that resumes after a long wait could
        make them again after the idempotency retention has expired, so that
        the duplicates would no longer be dropped.
        """
        outgoing: Outgoing | None = None
        session = self._current()
        try:
            db = await session.ready
            async with db.transaction() as handle:
                sends = _Sends() if self._has_outbox else None
                # A view's writes stay in its own session, so there is nothing to reset.
                tx = AttemptTransaction(handle, sends.record if sends is not None else _refused_send)
                if not self._host.shared:
                    _RESET.on(tx, lambda: self._reset(handle))
                try:
                    yield tx
                finally:
                    outgoing = sends.close() if sends is not None else None
                if self._has_outbox:
                    await self._commit_outbox(handle, outgoing)
        except BaseException:
            if session.ended:
                session.retired = True
            raise
        if outgoing is not None:
            await self.call(self._flush)

    async def _commit_outbox(self, handle: ConnectionTransaction, outgoing: Outgoing | None) -> None:
        """Write the outbox in ``handle``: the unstored committed sends, plus ``outgoing``, which the commit makes."""
        outbox = await self._unstored(handle)
        if outgoing is not None:
            outbox.append(outgoing)
            sent = outgoing
            handle.with_commit(lambda: self._make(sent))
        handle.set_record(OUTBOX, _encode_outbox(outbox))

    def _current(self) -> _Session:
        """Return the current session, opening a new one if there is none or it was retired."""
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
        """Return the object's schema, or ``None`` for the key's separate databases, which do not have it."""
        return self._config.schema if self._name is None else None

    async def _start(self) -> Connection:
        host = self._host
        # The page cache's namespace. The key is escaped so that no key can be
        # mistaken for another key's separate database (names have no `/`).
        key = f"{host.service}/{quote(host.key, safe='')}"
        namespace = key if self._name is None else f"{key}/{self._name}"
        store: Store = _ReaderStore(host, self._name) if host.shared else _ObjectStore(host, self._name)
        if self._config.wrap_store is not None:
            store = self._config.wrap_store(store)
        db = await open_connection(namespace, store, reader=host.shared)
        try:
            # Views write nothing: make a write fail, rather than vanish with the attempt.
            if host.shared:
                await db.execute("PRAGMA query_only = 1")
            if self._has_outbox:
                # Make the sends a crash left unmade. When this attempt reopens the
                # database, the sends it already made were stored first, so skip them.
                left = _decode_outbox(await db.record(OUTBOX))
                for outgoing in left:
                    if outgoing["id"] not in self._made:
                        self._make(outgoing, again=True)
                # Empty the outbox right away, as for a transaction's own sends.
                if left:
                    await self._flush(db)
            # Views too, although their writes are never stored: a key never written reads as empty tables.
            own = self._own_schema()
            if own is not None:
                await self._ensure_schemas(db, [own])
        except BaseException:
            await db.close()
            raise
        return db

    async def _end_session(self, error: BaseException | None) -> None:
        """End the session, then re-raise the handler's error, if any.

        The handler's error takes precedence: what it committed is stored, and
        the next open makes any sends left in the outbox. A failure to end the
        session (a transaction left open, say) fails a handler that succeeded,
        and a retry then ends it.
        """
        self._ended = True
        failed: BaseException | None = None
        try:
            await self._close()
        except BaseException as failure:  # pylint: disable=broad-exception-caught
            failed = failure
        if error is not None:
            raise error
        if failed is not None:
            raise failed

    async def _close(self) -> None:
        """Close the session as the attempt ends.

        A view's session just closes, as it only read its snapshot. Any other
        session is saved, after emptying its outbox. If a failure ended it, it
        is closed, and the object's own database is reopened and saved, since
        its outbox may hold sends not yet made (left by a crash, when this
        attempt's open failed) or made but not removed (when the commit's wait
        failed).
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
        """Empty the outbox and save the session, closing it if that fails."""
        try:
            if self._has_outbox:
                await self._flush(db)
            await db.save()
        except BaseException:
            with contextlib.suppress(Exception):
                await db.close()
            raise

    async def _reset(self, handle: ConnectionTransaction) -> None:
        """Empty the database in the transaction ``handle`` and reapply the schemas this attempt applied (see ``reset``)."""
        own = self._own_schema()
        schemas = ([own] if own is not None else []) + [s for s in self._ensured.values() if s is not own]
        await drop_all(handle, schemas)

    async def _ensure_schemas(self, db: Connection, schemas: Sequence[Schema]) -> None:
        """Apply the missing migrations of ``schemas``, checking each schema at most once per attempt."""
        lacking = [s for s in schemas if s.name not in self._ensured]
        if not lacking:
            return
        await apply(db, lacking, reader=self._host.shared)
        for schema in lacking:
            self._ensured[schema.name] = schema

    def _make(self, outgoing: Outgoing, again: bool = False) -> None:
        """Make the sends of a committed transaction, with idempotency keys so that repeats are dropped."""
        for index, send in enumerate(outgoing["sends"]):
            delay = send.get("delay")
            due = send.get("due")
            if delay is not None and again and due is not None:
                # When made again, a delayed send waits only for the rest of its delay.
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
        """Return the committed outbox without the transactions whose sends this attempt made (a later commit stores those)."""
        return [outgoing for outgoing in _decode_outbox(await db.record(OUTBOX)) if outgoing["id"] not in self._made]

    async def _flush(self, db: Connection) -> None:
        """Remove the sends this attempt made from the outbox, so that a later open does not make them again."""
        if not await db.record(OUTBOX):
            return
        # Read the outbox in the transaction that writes it, to keep sends committed in the meantime.
        async with db.transaction() as tx:
            tx.set_record(OUTBOX, _encode_outbox(await self._unstored(tx)))


async def with_attempt(handler: Callable[[], Awaitable[T]], attempt: Attempt) -> T:
    """Run ``handler``, then end the SQL session of ``attempt``, if it opened one (see ``Attempt.end``)."""
    try:
        value = await handler()
    except BaseException as error:
        await attempt.end(error)
        raise  # `end` already raises; this is for the type checker
    await attempt.end(None)
    return value
