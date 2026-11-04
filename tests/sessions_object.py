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
"""Session objects, their attempts run over an in-memory copy of the object's state, without Restate."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import asyncio
import itertools
import typing
from dataclasses import dataclass, field
from typing import Any

import pytest

import restate
from restate import sessions
from restate.context import AttemptFinishedEvent, Request
from restate.exceptions import TerminalError
from restate.sessions._object import invoke
from sessions_database import Backend  # pylint: disable=import-error


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _NeverFinished(AttemptFinishedEvent):
    def is_set(self) -> bool:
        return False

    async def wait(self):
        await asyncio.Event().wait()


_ids = itertools.count()


def request() -> Request:
    return Request(
        id=f"inv_test{next(_ids)}", headers={}, attempt_headers={}, body=b"", attempt_finished_event=_NeverFinished()
    )


@dataclass
class Send:
    service: str
    handler: str
    arg: bytes
    key: str | None
    delay: Any
    idempotency_key: str | None


class Answer(restate.RestateDurableCallFuture[bytes]):
    """A call's result, at once."""

    def __init__(self, value: bytes) -> None:
        super().__init__()
        self.value = value

    def __await__(self):
        async def value() -> bytes:
            return self.value

        return value().__await__()

    async def invocation_id(self) -> str:
        return "inv_call"

    async def cancel_invocation(self) -> None:
        pass


@dataclass
class Host:
    """An attempt's context over ``backend``, the object's state, as the SDK's gives it."""

    backend: Backend
    service: str = "notes"
    key: str = "alice"
    shared: bool = False
    sends: list[Send] = field(default_factory=list)
    calls: list[tuple[str, str, bytes, str | None, str | None]] = field(default_factory=list)
    answers: dict[str, bytes] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    # How many more syncs go through before the process crashes: every sync after fails, losing its writes.
    _syncs_left: int | None = None

    def crash_after(self, syncs: int) -> None:
        """Let ``syncs`` more syncs through, then crash: every later sync fails."""
        self._syncs_left = syncs

    def get(self, key: str):
        future = asyncio.get_running_loop().create_future()
        future.set_result(self.backend.request().get(key))
        return future

    def set(self, key: str, value: bytes) -> None:
        self.backend.request().set(key, value)

    def clear(self, key: str) -> None:
        self.backend.request().clear(key)

    async def state_keys(self) -> list[str]:
        return [*self.backend.stored, *(key for key, _ in self.backend.unconfirmed)]

    async def sync(self) -> None:
        if self._syncs_left is not None:
            if self._syncs_left == 0:
                self.backend.lose()
                raise RuntimeError("the sync failed")
            self._syncs_left -= 1
        self.backend.confirm()

    def generic_call(self, service, handler, arg, key=None, idempotency_key=None, headers=None):
        self.calls.append((service, handler, arg, key, idempotency_key))
        return Answer(self.answers[handler])

    def generic_send(self, service, handler, arg, key=None, send_delay=None, idempotency_key=None, headers=None):
        self.sends.append(Send(service, handler, arg, key, send_delay, idempotency_key))

    def cancel_invocation(self, invocation_id: str) -> None:
        self.cancelled.append(invocation_id)

    def attempt_end(self):
        return None

    def on_attempt_end(self, listener):
        return lambda: None

    def view(self) -> "Host":
        """A view's context of the same key."""
        return Host(self.backend, self.service, self.key, shared=True)


def run(obj: sessions.SessionObject, name: str, host: Host, *args: Any) -> typing.Awaitable[Any]:
    return invoke(obj, name, host, request(), *args)


# --- The objects --------------------------------------------------------------------------------

notes = sessions.SessionObject("notes", schema=["CREATE TABLE notes (text TEXT NOT NULL)"])


@notes.handler
async def add(ctx: sessions.SessionContext, text: str) -> None:
    await ctx.db.execute("INSERT INTO notes (text) VALUES (?)", [text])


@notes.view
async def texts(ctx: sessions.ViewContext) -> list[str]:
    return [typing.cast(str, row["text"]) for row in await ctx.db.query("SELECT text FROM notes ORDER BY rowid")]


audit = restate.VirtualObject("audit")


@audit.handler()
async def record(ctx: restate.ObjectContext, entry: str) -> None:
    pass


pricing = restate.Service("pricing")


@pricing.handler()
async def quote(ctx: restate.Context, sku: str) -> int:
    return 0


# --- Tests --------------------------------------------------------------------------------------


async def test_a_handler_commits_and_a_view_reads_what_it_committed():
    host = Host(Backend())
    await run(notes, "add", host, "milk")
    await run(notes, "add", host, "eggs")
    assert await run(notes, "texts", host.view()) == ["milk", "eggs"]


async def test_a_view_of_a_key_never_written_reads_empty_tables_and_stores_nothing():
    backend = Backend()
    assert await run(notes, "texts", Host(backend).view()) == []
    assert backend.stored == {}


async def test_a_step_added_later_runs_once_on_each_database():
    backend = Backend()
    v1 = sessions.SessionObject("evolving", schema=["CREATE TABLE t (a INTEGER)"])

    @v1.handler()
    async def put(ctx: sessions.SessionContext) -> None:
        await ctx.db.execute("INSERT INTO t (a) VALUES (1)")

    await run(v1, "put", Host(backend, service="evolving"))

    v2 = sessions.SessionObject("evolving", schema=["CREATE TABLE t (a INTEGER)", "ALTER TABLE t ADD COLUMN b TEXT"])

    @v2.handler()
    async def columns(ctx: sessions.SessionContext) -> list[str]:
        return [typing.cast(str, row["name"]) for row in await ctx.db.query("SELECT name FROM pragma_table_info('t')")]

    host = Host(backend, service="evolving")
    assert await run(v2, "columns", host) == ["a", "b"]
    assert await run(v2, "columns", host) == ["a", "b"]


async def test_the_sends_of_a_transaction_are_made_once_it_commits_and_never_if_it_rolls_back():
    shop = sessions.SessionObject("shop", schema=["CREATE TABLE orders (id TEXT PRIMARY KEY)"])

    @shop.handler()
    async def place(ctx: sessions.SessionContext, order: str) -> None:
        async with ctx.db.transaction() as tx:
            await tx.execute("INSERT INTO orders VALUES (?)", [order])
            tx.send(record, key="orders")(order)
            if order == "bad":
                raise ValueError("no")

    host = Host(Backend(), service="shop")
    await run(shop, "place", host, "o1")
    assert len(host.sends) == 1
    sent = host.sends[0]
    assert (sent.service, sent.handler, sent.key, sent.arg) == ("audit", "record", "orders", b'"o1"')
    assert sent.idempotency_key is not None and sent.idempotency_key.startswith("transaction/")
    with pytest.raises(ValueError):
        await run(shop, "place", host, "bad")
    assert len(host.sends) == 1
    # The outbox is empty: a later attempt makes nothing again.
    again = Host(host.backend, service="shop")
    await run(shop, "place", again, "o2")
    assert [send.arg for send in again.sends] == [b'"o2"']


async def test_the_sends_a_crash_left_in_the_outbox_are_made_again_with_the_same_keys():
    shop = sessions.SessionObject("shop2", schema=["CREATE TABLE orders (id TEXT PRIMARY KEY)"])

    @shop.handler()
    async def place(ctx: sessions.SessionContext, order: str) -> None:
        async with ctx.db.transaction() as tx:
            await tx.execute("INSERT INTO orders VALUES (?)", [order])
            tx.send(record, key="orders")(order)
            # The commit's sync goes through; then the process crashes, before the outbox is emptied.
            first.crash_after(1)

    backend = Backend()
    first = Host(backend, service="shop2")
    with pytest.raises(RuntimeError, match="the sync failed"):
        await run(shop, "place", first, "o1")
    assert len(first.sends) == 1

    # The next attempt opens the database as it starts, and makes them again.
    second = Host(backend, service="shop2")
    await run(shop, "place", second, "o2")
    keys = [send.idempotency_key for send in second.sends]
    assert keys[0] == first.sends[0].idempotency_key
    assert [send.arg for send in second.sends] == [b'"o1"', b'"o2"']

    third = Host(backend, service="shop2")
    await run(shop, "place", third, "o3")
    assert [send.arg for send in third.sends] == [b'"o3"']


async def test_an_sql_error_fails_the_handler_with_a_terminal_error():
    host = Host(Backend())
    broken = sessions.SessionObject("notes", schema=["CREATE TABLE notes (text TEXT NOT NULL)"])

    @broken.handler()
    async def bad(ctx: sessions.SessionContext) -> None:
        await ctx.db.execute("SELECT * FROM missing")

    with pytest.raises(TerminalError, match="no such table") as error:
        await run(broken, "bad", host)
    assert isinstance(error.value.__cause__, sessions.SqlError)


async def test_a_call_to_ctx_db_inside_its_transaction_is_refused():
    nested = sessions.SessionObject("nested", schema=["CREATE TABLE t (a INTEGER)"])

    @nested.handler()
    async def inside(ctx: sessions.SessionContext) -> None:
        async with ctx.db.transaction():
            await ctx.db.execute("INSERT INTO t VALUES (1)")

    with pytest.raises(TerminalError, match="use the transaction handle"):
        await run(nested, "inside", Host(Backend(), service="nested"))


async def test_a_view_writes_nothing():
    host = Host(Backend())
    await run(notes, "add", host, "milk")
    writer = sessions.SessionObject("notes", schema=["CREATE TABLE notes (text TEXT NOT NULL)"])

    @writer.view()
    async def sneaky(ctx: sessions.ViewContext) -> None:
        await ctx.db.query("INSERT INTO notes (text) VALUES ('x')")

    with pytest.raises(TerminalError):
        await run(writer, "sneaky", host.view())
    assert await run(notes, "texts", host.view()) == ["milk"]


async def test_calls_are_typed_by_their_handler_and_checked_for_their_key():
    caller = sessions.SessionObject("caller")

    @caller.handler()
    async def price(ctx: sessions.SessionContext, sku: str) -> int:
        return await ctx.rpc(quote, idempotency_key="k")(sku)

    @caller.handler()
    async def unkeyed(ctx: sessions.SessionContext) -> None:
        ctx.send(record)("x")

    @caller.handler()
    async def literal(ctx: sessions.SessionContext) -> Any:
        return await ctx.rpc.generic("pricing", "quote", "pear")

    host = Host(Backend(), service="caller", answers={"quote": b"42"})
    assert await run(caller, "price", host, "apple") == 42
    assert host.calls[0] == ("pricing", "quote", b'"apple"', None, "k")
    with pytest.raises(TerminalError, match="audit is an object: call it with its key"):
        await run(caller, "unkeyed", host)
    assert await run(caller, "literal", host) == 42


async def test_ctx_send_is_made_at_once_and_cancel_takes_an_invocation_id():
    sender = sessions.SessionObject("sender")

    @sender.handler()
    async def go(ctx: sessions.SessionContext) -> None:
        ctx.send(record, key="k")("entry")
        ctx.cancel("inv_1")
        ctx.cancel("not-an-id")

    host = Host(Backend(), service="sender")
    with pytest.raises(TerminalError, match="not an invocation id"):
        await run(sender, "go", host)
    assert [(s.handler, s.key, s.idempotency_key) for s in host.sends] == [("record", "k", None)]
    assert host.cancelled == ["inv_1"]


async def test_another_database_of_the_key_is_apart_from_the_objects_own():
    multi = sessions.SessionObject("multi", schema=["CREATE TABLE own (a INTEGER)"])

    @multi.handler()
    async def both(ctx: sessions.SessionContext) -> list[str]:
        other = sessions.database(ctx.db, "other")
        await other.execute("CREATE TABLE apart (b INTEGER)")
        rows = await other.query("SELECT name FROM sqlite_schema WHERE type = 'table'")
        return [typing.cast(str, row["name"]) for row in rows]

    host = Host(Backend(), service="multi")
    assert await run(multi, "both", host) == ["apart"]
    assert any(key.startswith("v22/db/other/") for key in host.backend.stored)


async def test_reset_empties_the_database_and_applies_the_schema_again():
    resettable = sessions.SessionObject("resettable", schema=["CREATE TABLE t (a INTEGER)"])

    @resettable.handler()
    async def fill(ctx: sessions.SessionContext) -> None:
        await ctx.db.execute("CREATE TABLE extra (b INTEGER)")
        await ctx.db.execute("INSERT INTO t VALUES (1)")

    @resettable.handler()
    async def empty(ctx: sessions.SessionContext) -> list[Any]:
        async with ctx.db.transaction() as tx:
            await sessions.reset(tx)
            await tx.execute("INSERT INTO t VALUES (2)")
        return await ctx.db.values("SELECT name FROM sqlite_schema WHERE type = 'table' UNION ALL SELECT a FROM t")

    host = Host(Backend(), service="resettable")
    await run(resettable, "fill", host)
    assert await run(resettable, "empty", host) == [["t"], [2]]


def test_an_sdk_handler_is_refused():
    plain = restate.VirtualObject("plain")

    @plain.handler()
    async def journaled(ctx: restate.ObjectContext) -> None:
        pass

    obj = sessions.SessionObject("refusing")
    with pytest.raises(TypeError, match="is an SDK handler"):
        obj.handler()(journaled)


def test_a_session_object_runs_its_handlers_with_explicit_recovery_and_lazy_state():
    assert notes.handlers["add"].explicit_recovery
    assert notes.handlers["texts"].explicit_recovery
    assert notes.handlers["texts"].kind == "shared"
    assert notes.enable_lazy_state
    assert notes.service_tag.metadata is not None
    assert notes.service_tag.metadata["dev.restate.tooling.journal.hidden.name.prefix"] == "v22/"


def test_a_handler_or_view_is_named_after_its_function_or_as_its_option_says():
    named = sessions.SessionObject("named")

    @named.handler(name="put")
    async def put_one(ctx: sessions.SessionContext, value: int) -> None:
        pass

    @named.view("peek")
    async def peek_one(ctx: sessions.ViewContext) -> None:
        pass

    assert sorted(named.handlers) == ["peek", "put"]
    assert named.handlers["peek"].kind == "shared"


async def test_the_object_store_issues_its_sync_and_listing_when_called():
    from restate.sessions._session import _ObjectStore  # pylint: disable=import-outside-toplevel

    issued: list[str] = []

    class Counting(Host):
        def state_keys(self):  # type: ignore[override]
            issued.append("keys")
            return super().state_keys()

        def sync(self):  # type: ignore[override]
            issued.append("sync")
            return super().sync()

    store = _ObjectStore(Counting(Backend()), "other")
    sync, keys = store.sync(), store.keys()
    assert issued == ["sync", "keys"]
    await sync
    assert await keys == []


async def test_cancelled_takes_the_ctx_of_a_handler_with_explicit_cancellation():
    obj = sessions.SessionObject("cancelling")

    @obj.handler
    async def implicit(ctx: sessions.SessionContext) -> None:
        await sessions.cancelled(ctx)

    @obj.handler(explicit_cancellation=True)
    async def explicit(ctx: sessions.SessionContext) -> None:
        await sessions.cancel_calls(ctx)

    assert not obj.handlers["implicit"].explicit_cancellation
    assert obj.handlers["explicit"].explicit_cancellation
    # Over a test's own host there is no SDK context to cancel.
    with pytest.raises(TerminalError, match="takes a session object's ctx"):
        await run(obj, "explicit", Host(Backend(), service="cancelling"))
