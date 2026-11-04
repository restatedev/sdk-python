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
"""A session's database over an in-memory store: SQL, values, records, transactions, and lost writes."""

# pylint: disable=missing-function-docstring,redefined-builtin

import asyncio
import itertools
import random
import typing

import pytest

from restate.sessions._database import SqlError, open


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Backend:
    """Stored values, and writes issued but not yet synced."""

    def __init__(self, seed=None):
        self.stored = {}
        self.unconfirmed = []
        # Which unsynced writes a failure keeps: none, or a random prefix.
        self.random = None if seed is None else random.Random(seed)

    def confirm(self):
        for key, value in self.unconfirmed:
            if value is None:
                self.stored.pop(key, None)
            else:
                self.stored[key] = value
        self.unconfirmed.clear()

    def lose(self):
        """A failure: the store keeps a prefix of the unsynced writes, as Restate does."""
        kept = 0 if self.random is None else self.random.randint(0, len(self.unconfirmed))
        del self.unconfirmed[kept:]
        self.confirm()

    def request(self, fails=False):
        """A request's view: its own writes over the stored values."""
        return Request(self, fails)


class Request:
    # A listing of the store's keys, if a test gives it one.
    keys: typing.Any = None

    def __init__(self, backend, fails):
        self.backend = backend
        self.fails = fails

    def get(self, key):
        # The read is issued now: what it sees is decided here.
        for k, value in reversed(self.backend.unconfirmed):
            if k == key:
                return value
        return self.backend.stored.get(key)

    def set(self, key, value):
        self.backend.unconfirmed.append((key, bytes(value)))

    def clear(self, key):
        self.backend.unconfirmed.append((key, None))

    async def sync(self):
        if self.fails:
            self.backend.lose()
            raise RuntimeError("the request failed")
        self.backend.confirm()


_namespaces = itertools.count()


def fresh():
    return f"namespace-{next(_namespaces)}"


async def test_runs_sql_and_round_trips_values():
    backend = Backend()
    ns = fresh()
    db = await open(ns, backend.request())
    await db.execute("CREATE TABLE t(i INTEGER, r REAL, s TEXT, b BLOB, n INTEGER)")
    await db.execute("INSERT INTO t VALUES(?, ?, ?, ?, ?)", [42, 1.5, "héllo", b"\x01\x02\x03", None])
    await db.execute("INSERT INTO t VALUES(?, ?, ?, ?, ?)", [2**62, -0.25, "", bytearray(), True])
    rows = await db.values("SELECT * FROM t ORDER BY i")
    assert rows == [[42, 1.5, "héllo", b"\x01\x02\x03", None], [2**62, -0.25, "", b"", 1]]
    assert await db.query_columns("SELECT i AS answer FROM t WHERE i = ?", [42]) == (["answer"], [[42]])
    assert await db.query("SELECT i, s FROM t ORDER BY i") == [{"i": 42, "s": "héllo"}, {"i": 2**62, "s": ""}]
    assert await db.get("SELECT i FROM t WHERE i = ?", [42]) == {"i": 42}
    assert await db.get("SELECT i FROM t WHERE i = 0") is None
    await db.save()

    # Another session, and another process's view (no cached state), read it.
    for name in [ns, fresh()]:
        again = await open(name, backend.request())
        assert await again.values("SELECT count(*) FROM t") == [[2]]
        await again.save()


async def test_a_column_named_twice_is_an_sql_error_for_query_but_not_for_values():
    db = await open(fresh(), Backend().request())
    with pytest.raises(SqlError, match="named twice"):
        await db.query("SELECT 1 AS a, 2 AS a")
    assert await db.values("SELECT 1 AS a, 2 AS a") == [[1, 2]]
    await db.save()


async def test_values_it_cannot_store_are_sql_errors():
    db = await open(fresh(), Backend().request())
    with pytest.raises(SqlError, match="exceeds 64 bits"):
        await db.values("SELECT ?", [2**64])
    with pytest.raises(SqlError, match="unsupported SQL value"):
        await db.values("SELECT ?", [object()])  # type: ignore[list-item]
    with pytest.raises(SqlError, match="a sequence of values"):
        await db.values("SELECT ?", "x")  # type: ignore[arg-type]
    assert db.is_usable()
    await db.save()


async def test_an_sql_error_leaves_the_session_usable_and_a_transaction_rolls_back():
    db = await open(fresh(), Backend().request())
    await db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
    with pytest.raises(SqlError, match="no such table"):
        await db.values("SELECT * FROM missing")
    assert db.is_usable()
    with pytest.raises(ValueError, match="abort"):
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO t VALUES(1)")
            raise ValueError("abort")
    assert await db.values("SELECT count(*) FROM t") == [[0]]
    async with db.transaction() as tx:
        assert await tx.values("INSERT INTO t VALUES(2) RETURNING id") == [[2]]
    assert await db.values("SELECT id FROM t") == [[2]]
    await db.save()


async def consistent_after_loss(lost, seed):
    """
    Requests insert a row each; request ``lost`` fails, its sync losing some of
    its writes (none kept, or a prefix by ``seed``). The owner and a process
    without cached state must then read the rows of the other requests, and the
    lost one's only if both do.
    """
    backend = Backend(seed)
    ns = fresh()
    db = await open(ns, backend.request())
    await db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v BLOB)")
    await db.save()
    for id in range(20):
        fails = id == lost
        db = await open(ns, backend.request(fails))
        insert = db.execute("INSERT INTO t VALUES(?, randomblob(3000))", [id])
        if fails:
            # The store's own error, as it raised it.
            with pytest.raises(RuntimeError, match="the request failed"):
                await insert
            await db.close()
        else:
            await insert
            await db.save()
    seen = []
    for name in [ns, fresh()]:
        view = await open(name, backend.request())
        [[rows, landed]] = await view.values("SELECT count(*), count(*) FILTER (WHERE id = ?) FROM t", [lost])
        [[integrity]] = await view.values("PRAGMA integrity_check")
        await view.save()
        if rows != 19 + typing.cast(int, landed) or integrity != "ok":
            return False
        seen.append(landed)
    return seen[0] == seen[1]


async def test_lost_writes_leave_every_reader_consistent():
    for seed in [None, 1, 2, 3, 4, 5]:
        for lost in range(1, 15):
            assert await consistent_after_loss(lost, seed), f"lost request {lost}, seed {seed}"


async def test_a_write_the_store_rejects_fails_the_next_sync_with_its_error():
    store = Backend().request()

    class Closed(Exception):
        pass

    def rejected(key, value):
        raise Closed("state is closed")

    store.set = rejected
    db = await open(fresh(), store)
    with pytest.raises(Closed, match="state is closed"):
        await db.execute("CREATE TABLE t(id INTEGER)")
    assert not db.is_usable()
    await db.close()


async def test_a_transaction_holds_the_session_other_calls_wait_until_it_ends():
    db = await open(fresh(), Backend().request())
    await db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
    handles = []

    async def rolled_back():
        async with db.transaction() as tx:
            handles.append(tx)
            await tx.execute("INSERT INTO t VALUES(1)")
            await asyncio.sleep(0.01)
            raise ValueError("abort")

    async def committed():
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO t VALUES(3)")

    first = asyncio.ensure_future(rolled_back())
    await asyncio.sleep(0)
    # Started while the transaction waits: neither runs inside it.
    write = asyncio.ensure_future(db.execute("INSERT INTO t VALUES(2)"))
    last = asyncio.ensure_future(committed())
    with pytest.raises(ValueError, match="abort"):
        await first
    await write
    await last
    assert await db.values("SELECT id FROM t ORDER BY id") == [[2], [3]]
    with pytest.raises(SqlError, match="the transaction has ended"):
        await handles[0].values("SELECT 1")
    await db.save()


async def test_the_calls_of_a_body_run_in_call_order_and_before_its_commit():
    db = await open(fresh(), Backend().request())
    await db.execute("CREATE TABLE t(id INTEGER)")
    async with db.transaction() as tx:
        # Made together, and one left unawaited: each runs, in order, before the commit.
        pending = asyncio.ensure_future(tx.execute("INSERT INTO t VALUES(3)"))
        await asyncio.gather(*(tx.execute("INSERT INTO t VALUES(?)", [i]) for i in (1, 2)))
    assert pending.done()
    assert await db.values("SELECT id FROM t ORDER BY rowid") == [[3], [1], [2]]
    await db.save()


async def test_a_commit_that_fails_rolls_the_transaction_back():
    db = await open(fresh(), Backend().request())
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("CREATE TABLE p(id INTEGER PRIMARY KEY)")
    await db.execute("CREATE TABLE c(pid INTEGER REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED)")
    # The deferred constraint fails at COMMIT, not at the INSERT.
    with pytest.raises(SqlError, match="FOREIGN KEY constraint failed"):
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO c VALUES(7)")
    assert db.is_usable()
    # Nothing of it is left, and the next transaction can begin.
    assert await db.values("SELECT * FROM c") == []
    async with db.transaction() as tx:
        await tx.execute("INSERT INTO p VALUES(7)")
    assert await db.values("SELECT id FROM p") == [[7]]
    await db.save()


async def test_a_statement_that_ends_the_transaction_of_a_body_ends_the_session_and_commits_nothing():
    backend = Backend()
    ns = fresh()
    db = await open(ns, backend.request())
    await db.execute("CREATE TABLE t(id INTEGER)")
    for end in ["COMMIT", "ROLLBACK", "END"]:
        session = await open(ns, backend.request())
        with pytest.raises(SqlError, match="ended the transaction"):
            async with session.transaction() as tx:
                await tx.execute("INSERT INTO t VALUES(1)")
                await tx.execute(end)
        assert not session.is_usable()
    assert await db.values("SELECT count(*) FROM t") == [[0]]
    await db.save()


async def test_save_refuses_a_transaction_left_open_whose_changes_are_lost():
    backend = Backend()
    ns = fresh()
    db = await open(ns, backend.request())
    await db.execute("CREATE TABLE t(id INTEGER)")
    await db.execute("BEGIN")
    await db.execute("INSERT INTO t VALUES(1)")
    with pytest.raises(SqlError, match="left open"):
        await db.save()
    again = await open(ns, backend.request())
    assert await again.values("SELECT count(*) FROM t") == [[0]]
    await again.save()


async def test_a_record_a_commit_cannot_hold_is_refused_and_the_session_goes_on():
    db = await open(fresh(), Backend().request())
    await db.execute("CREATE TABLE t(id INTEGER)")
    with pytest.raises(SqlError):
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO t VALUES(1)")
            tx.set_record("outbox", bytes((16 << 20) + 1))
    assert db.is_usable()
    assert await db.values("SELECT count(*) FROM t") == [[0]]
    await db.save()


async def test_a_call_running_meanwhile_does_not_make_the_session_look_unusable():
    db = await open(fresh(), Backend().request())
    await db.execute("CREATE TABLE t(id INTEGER)")
    running = asyncio.ensure_future(
        db.values(
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 200000) SELECT count(*) FROM n"
        )
    )
    await asyncio.sleep(0)
    assert db.is_usable()
    await running
    await db.save()
    assert not db.is_usable()


async def test_records_commit_with_their_transaction_and_only_then():
    backend = Backend()
    namespace = fresh()
    db = await open(namespace, backend.request())
    await db.execute("CREATE TABLE t(id INTEGER)")
    assert await db.record("outbox") == b""
    # Even with a transaction that changes nothing.
    async with db.transaction() as tx:
        await tx.values("SELECT * FROM t")
        tx.set_record("outbox", b"sends 1")
        assert await tx.record("outbox") == b"sends 1"
    assert await db.record("outbox") == b"sends 1"
    # A rollback commits none; nor does a commit that sets none change them.
    with pytest.raises(ValueError, match="no"):
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO t VALUES(2)")
            tx.set_record("outbox", b"sends 2")
            raise ValueError("no")
    async with db.transaction() as tx:
        await tx.execute("INSERT INTO t VALUES(3)")
    assert await db.record("outbox") == b"sends 1"
    await db.save()
    # A later session opens with them; an empty value reads as never set.
    again = await open(namespace, backend.request())
    assert await again.record("outbox") == b"sends 1"
    async with again.transaction() as tx:
        tx.set_record("outbox", b"")
    assert await again.record("outbox") == b""
    await again.save()


async def test_with_commit_runs_after_the_commits_writes_before_its_sync_and_never_on_a_rollback():
    backend = Backend()
    events = []

    class Recording(Request):
        def set(self, key, value):
            events.append("set")
            super().set(key, value)

        async def sync(self):
            events.append("sync")
            await super().sync()

    db = await open(fresh(), Recording(backend, False))
    await db.execute("CREATE TABLE t(id INTEGER)")
    events.clear()
    async with db.transaction() as tx:
        await tx.execute("INSERT INTO t VALUES(1)")
        tx.with_commit(lambda: events.append("sent"))
    assert events[-2:] == ["sent", "sync"] and events[0] == "set"

    events.clear()
    with pytest.raises(ValueError):
        async with db.transaction() as tx:
            await tx.execute("INSERT INTO t VALUES(2)")
            tx.with_commit(lambda: events.append("sent"))
            raise ValueError("no")
    assert "sent" not in events

    # A transaction that writes nothing syncs nothing: it runs after the commit.
    events.clear()
    async with db.transaction() as tx:
        tx.with_commit(lambda: events.append("sent"))
    assert events == ["sent"]
    await db.save()


async def test_a_call_to_the_session_inside_its_transaction_is_refused_not_left_waiting():
    db = await open(fresh(), Backend().request())
    await db.execute("CREATE TABLE t(id INTEGER)")

    async def after(delay):
        await asyncio.sleep(delay)
        await db.execute("INSERT INTO t VALUES(3)")

    async with db.transaction() as tx:
        await tx.execute("INSERT INTO t VALUES(1)")
        # It would wait for this transaction, which waits for this body.
        with pytest.raises(SqlError, match="use the transaction handle"):
            await db.execute("INSERT INTO t VALUES(2)")
        with pytest.raises(SqlError, match="use the transaction handle"):
            async with db.transaction():
                pass
        # Once the body has ended, a task it started may use the session.
        later = asyncio.ensure_future(after(0.005))
    await later
    assert await db.values("SELECT id FROM t ORDER BY id") == [[1], [3]]
    await db.save()


async def test_a_call_to_a_session_inside_a_transaction_of_another_inside_its_own_is_refused():
    a = await open(fresh(), Backend().request())
    b = await open(fresh(), Backend().request())
    async with a.transaction():
        async with b.transaction() as tx:
            await tx.values("SELECT 1")
            # `a` waits for its transaction, which waits for this body.
            with pytest.raises(SqlError, match="use the transaction handle"):
                await a.values("SELECT 1")
        # Once b's transaction has ended, b may be used again from a's body.
        assert await b.values("SELECT 1") == [[1]]
    await a.save()
    await b.save()


async def test_a_statement_that_commits_completes_once_its_writes_are_stored_however_many():
    backend = Backend()
    db = await open(fresh(), backend.request())
    await db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, b BLOB)")
    # Thousands of page writes: a tail entry of megabytes, in parts, each
    # scheduled on the loop.
    await db.execute(
        "INSERT INTO t(b) SELECT randomblob(3000) FROM "
        "(WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 3000) SELECT x FROM c)"
    )
    assert backend.unconfirmed == []
    assert sum(key.startswith("v22/tail/") for key in backend.stored) > 8
    await db.save()


async def test_an_awaitable_get_is_read_when_issued():
    """A store whose get returns an awaitable, as a Restate context's does."""
    backend = Backend()
    plain = backend.request()

    class AwaitableGets(Request):
        def get(self, key):
            future = asyncio.get_running_loop().create_future()
            future.set_result(plain.get(key))
            return future

    ns = fresh()
    db = await open(ns, AwaitableGets(backend, False))
    await db.execute("CREATE TABLE t(id INTEGER)")
    await db.execute("INSERT INTO t VALUES(1)")
    await db.save()
    for name in [ns, fresh()]:
        again = await open(name, AwaitableGets(backend, False))
        assert await again.values("SELECT * FROM t") == [[1]]
        await again.save()


class ReadOnly(Request):
    """A request that refuses writes, as a shared handler's store does."""

    def set(self, key, value):
        raise AssertionError("a reader writes nothing")

    def clear(self, key):
        raise AssertionError("a reader writes nothing")


async def test_a_reader_reads_the_state_it_opened_on_beside_the_writer():
    backend = Backend()
    ns = fresh()
    writer = await open(ns, backend.request())
    await writer.execute("CREATE TABLE t(n INTEGER)")
    await writer.execute("INSERT INTO t VALUES (1)")

    reader = await open(ns, ReadOnly(backend, False), reader=True)
    assert await reader.values("SELECT n FROM t") == [[1]]
    # The writer commits again: the reader reads on, the state it opened on.
    await writer.execute("INSERT INTO t VALUES (2)")
    assert await reader.values("SELECT n FROM t") == [[1]]
    await reader.close()

    again = await open(ns, ReadOnly(backend, False), reader=True)
    assert await again.values("SELECT n FROM t ORDER BY n") == [[1], [2]]
    await again.close()
    await writer.save()


def listing(backend):
    """As the object's ctx.state_keys(): every key, those not yet synced too."""
    return lambda: [*backend.stored, *(key for key, _ in backend.unconfirmed)]


async def packs_after_commits(keys):
    """The pack parts left after 100 commits that each replace a row."""
    backend = Backend()
    store = backend.request()
    if keys:
        store.keys = listing(backend)
    db = await open(fresh(), store)
    await db.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, b BLOB)")
    for _ in range(100):
        await db.execute("INSERT OR REPLACE INTO t VALUES(1, randomblob(3000))")
    await db.save()
    return sum(key.startswith("v22/pack/") for key in backend.stored)


async def test_a_store_that_lists_its_keys_is_collected_and_one_that_cannot_keeps_its_packs():
    swept, kept = await packs_after_commits(True), await packs_after_commits(False)
    assert swept < 5 and kept > 10, (swept, kept)


def raises():
    raise RuntimeError("listing failed")


async def rejects():
    raise RuntimeError("listing failed")


@pytest.mark.parametrize("keys", [raises, rejects])
async def test_a_listing_that_fails_ends_the_session_after_the_commit_that_collects_which_landed(keys):
    backend = Backend()
    ns = fresh()
    store = backend.request()
    store.keys = keys
    db = await open(ns, store)
    await db.execute("CREATE TABLE t(b BLOB)")
    committed = 0
    # A commit that checkpoints collects: once the tail holds a few rows. It
    # returns, and the next call fails with the listing's error.
    with pytest.raises(RuntimeError, match="listing failed"):
        for _ in range(40):
            await db.execute("INSERT INTO t VALUES (randomblob(3000))")
            committed += 1
    assert not db.is_usable()
    reopened = await open(ns, backend.request())
    assert await reopened.values("SELECT count(*) FROM t") == [[committed]]


async def test_a_store_that_answers_with_the_wrong_type_fails_the_request_with_why():
    store = Backend().request()
    store.get = lambda key: "not bytes"  # type: ignore[method-assign,assignment,return-value]
    with pytest.raises(RuntimeError, match="expected bytes, got str"):
        await open(fresh(), store)
