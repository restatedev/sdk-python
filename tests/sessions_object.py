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
from restate.sessions import internal
from restate.context import AttemptFinishedEvent, Request
from restate.discovery import compute_discovery
from restate.endpoint import Endpoint
from restate.exceptions import TerminalError
from restate.handler import handler_from_callable
from restate.sessions._object import HEAD_KEY, invoke
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

    # No VM: the database's store goes through get, set and clear.
    vm = None

    def wait(self, handle: int):
        raise AssertionError("a host without a VM waits for no read")

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
        other = internal.database(ctx.db, "other")
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
            await internal.reset(tx)
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
    with pytest.raises(TypeError, match="is a handler already"):
        obj.handler()(journaled)
    # A session object's handler too: it is registered once.
    with pytest.raises(TypeError, match="is a handler already"):
        obj.handler()(add)


def test_a_session_object_runs_its_handlers_with_explicit_recovery_and_its_head_preloaded():
    assert notes.handlers["add"].explicit_recovery
    assert notes.handlers["texts"].explicit_recovery
    assert notes.handlers["texts"].kind == "shared"
    # Restate sends the database's head with each invocation, and the rest of the state as it is read.
    manifest = compute_discovery(Endpoint().bind(notes), "bidi").services[0]
    assert manifest.enableLazyState
    assert manifest.eagerStateKeysWhitelist == [HEAD_KEY]
    assert all(handler.eagerStateKeysWhitelist is None for handler in manifest.handlers)
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
        await internal.cancelled(ctx)

    @obj.handler(explicit_cancellation=True)
    async def explicit(ctx: sessions.SessionContext) -> None:
        await internal.cancel_calls(ctx)

    assert not obj.handlers["implicit"].explicit_cancellation
    assert obj.handlers["explicit"].explicit_cancellation
    # Over a test's own host there is no SDK context to cancel.
    with pytest.raises(TerminalError, match="takes a session object's ctx"):
        await run(obj, "explicit", Host(Backend(), service="cancelling"))


async def test_cancelled_ends_its_waiter_cancelled_when_the_attempt_ends_first(monkeypatch):
    from restate import _preview  # pylint: disable=import-outside-toplevel
    from restate.exceptions import SuspendedException  # pylint: disable=import-outside-toplevel
    from restate.sessions import _calls, _object, _scope  # pylint: disable=import-outside-toplevel

    async def attempt_ended(_sdk: Any) -> None:
        raise SuspendedException()

    monkeypatch.setattr(_preview, "cancellation", attempt_ended)
    host = Host(Backend())
    ctx = _object.AttemptContext(
        key="k",
        request=request(),
        db=typing.cast(Any, None),
        rpc=_calls.Caller(host),
        send=_calls.Sender(lambda send: None),
        cancel=lambda invocation_id: None,
    )
    _scope.SCOPE.on(ctx, _scope.Scope(sdk=typing.cast(Any, object()), explicit_cancellation=True))
    watcher = asyncio.ensure_future(internal.cancelled(ctx))
    await asyncio.wait({watcher})
    # Cancelled, not failed: asyncio reports no exception nobody retrieved.
    assert watcher.cancelled()


class FakeDatabase:
    """A test's own database, as ``sessions.Database`` declares one: it records the SQL it is given."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []

    async def query(self, sql: str, params: Any = None) -> list[sessions.Row]:
        self.statements.append((sql, params))
        return []

    async def get(self, sql: str, params: Any = None) -> sessions.Row | None:
        return None

    async def values(self, sql: str, params: Any = None) -> sessions.Rows:
        return []

    async def query_columns(self, sql: str, params: Any = None) -> tuple[list[str], sessions.Rows]:
        return [], []

    async def execute(self, sql: str, params: Any = None) -> None:
        self.statements.append((sql, params))

    def transaction(self) -> typing.AsyncContextManager[sessions.Transaction]:
        raise NotImplementedError


@dataclass
class FakeContext:
    """A test's own handler context, as ``sessions.SessionContext`` declares one."""

    db: FakeDatabase
    key: str = "alice"
    request: Request = field(default_factory=request)
    rpc: sessions.Rpc = field(default_factory=lambda: typing.cast(sessions.Rpc, None))
    send: sessions.Send = field(default_factory=lambda: typing.cast(sessions.Send, None))
    cancel: sessions.Cancel = lambda invocation_id: None


async def test_a_handler_runs_over_a_fake_context_of_a_test():
    fake = FakeContext(db=FakeDatabase())
    # The protocols are structural: the fake is a SessionContext to the type checkers.
    ctx: sessions.SessionContext = fake
    await add(ctx, "milk")
    assert fake.db.statements == [("INSERT INTO notes (text) VALUES (?)", ["milk"])]
    # And the function is still the handler that calls and the client name.
    assert handler_from_callable(add).name == "add"


TURNS: internal.ScopeKey[list[str]] = internal.ScopeKey("the turns of a test")


async def test_a_scope_key_keeps_its_value_for_the_attempt_and_a_new_attempt_starts_without_it():
    scoped = sessions.SessionObject("scoped")

    @scoped.handler
    async def turn(ctx: sessions.SessionContext, text: str) -> list[str]:
        seen_before = TURNS.get(ctx)
        turns = TURNS.of(ctx, list)
        turns.append(text)
        # `of` makes it once: the same list, for the attempt.
        assert TURNS.of(ctx, list) is turns
        return [*(seen_before or []), *turns]

    @scoped.view
    async def peek(ctx: sessions.ViewContext) -> list[str]:
        TURNS.set(ctx, ["view"])
        value = TURNS.get(ctx)
        TURNS.delete(ctx)
        assert TURNS.get(ctx) is None
        return value or []

    host = Host(Backend(), service="scoped")
    assert await run(scoped, "turn", host, "a") == ["a"]
    # Another attempt, another scope: it starts without what the first kept.
    assert await run(scoped, "turn", host, "b") == ["b"]
    assert await run(scoped, "peek", host.view()) == ["view"]


async def test_definition_of_gives_the_session_object_of_a_handler_or_a_view():
    defined = sessions.SessionObject("defined")

    @defined.handler
    async def handled(ctx: sessions.SessionContext) -> bool:
        return internal.definition_of(ctx) is defined

    @defined.view
    async def viewed(ctx: sessions.ViewContext) -> bool:
        return internal.definition_of(ctx) is defined

    host = Host(Backend(), service="defined")
    assert await run(defined, "handled", host)
    assert await run(defined, "viewed", host.view())


def test_a_scope_key_takes_the_context_of_a_session_object():
    with pytest.raises(TypeError, match="takes the context of a session object's handler or view"):
        TURNS.get(FakeContext(db=FakeDatabase()))
    assert internal.definition_of(FakeContext(db=FakeDatabase())) is None


def test_served_adds_what_each_service_needs_once_and_serve_only_picks():
    companion = restate.VirtualObject("companion")
    helper = restate.Service("helper")
    first = internal.needing(sessions.SessionObject("first"), lambda: [companion, helper])
    second = internal.needing(sessions.SessionObject("second"), lambda: [companion])
    internal.needing(companion, lambda: [helper])

    assert [s.name for s in internal.served([first, second])] == ["first", "companion", "helper", "second"]
    assert [s.name for s in internal.served([first, second], "second, helper")] == ["helper", "second"]
    with pytest.raises(ValueError, match="RESTATE_SERVE_ONLY names nowhere"):
        internal.served([first], "nowhere")


def test_app_serves_what_restate_serve_only_names(monkeypatch):
    companion = restate.VirtualObject("companion2")
    first = internal.needing(sessions.SessionObject("first2"), lambda: [companion])
    monkeypatch.setenv("RESTATE_SERVE_ONLY", "companion2")
    assert callable(sessions.app(first))
    monkeypatch.setenv("RESTATE_SERVE_ONLY", "unknown")
    with pytest.raises(ValueError, match="RESTATE_SERVE_ONLY names unknown"):
        sessions.app(first)


async def test_an_attempt_is_freed_once_it_ends():
    import gc  # pylint: disable=import-outside-toplevel
    import weakref  # pylint: disable=import-outside-toplevel

    kept: list[weakref.ref[Any]] = []
    freed = sessions.SessionObject("freed", schema=["CREATE TABLE t (a INTEGER)"])

    @freed.handler
    async def use(ctx: sessions.SessionContext) -> None:
        await internal.database(ctx.db, "other").execute("CREATE TABLE IF NOT EXISTS u (b INTEGER)")
        kept.append(weakref.ref(ctx.db))

    await run(freed, "use", Host(Backend(), service="freed"))
    gc.collect()
    # Nothing of this package's holds an attempt's database (and so the attempt) past it.
    assert kept[0]() is None
