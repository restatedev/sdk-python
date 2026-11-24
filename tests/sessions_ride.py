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
"""A write that rides the next commit of a database (``ride_commit``): written in that commit's transaction, before
its commit, so that it costs no commit of its own; ``flush_riders`` writes what is still pending."""

# pylint: disable=missing-function-docstring,missing-class-docstring,protected-access

import asyncio
import gc
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from restate import sessions
from restate.sessions import _session, internal
from restate.sessions.internal import flush_riders, ride_commit
from sessions_database import Backend  # pylint: disable=import-error
from sessions_object import Host, request  # pylint: disable=import-error


@pytest.fixture
def anyio_backend():
    return "asyncio"


@dataclass
class Counting(Host):
    """A host that counts its syncs: each a commit."""

    syncs: int = 0

    async def sync(self) -> None:
        await super().sync()
        self.syncs += 1


Write = Callable[[Any], Awaitable[None]]


def insert(x: str) -> Write:
    async def write(tx: Any) -> None:
        await tx.execute("INSERT INTO t VALUES (?)", [x])

    return write


async def rows(db: Any) -> list[str]:
    return [row[0] for row in await db.values("SELECT x FROM t ORDER BY x")]


async def hops(n: int) -> None:
    """Let ``n`` event-loop iterations pass."""
    for _ in range(n):
        await asyncio.sleep(0)


async def until(condition: Callable[[], bool]) -> None:
    """Wait until ``condition`` holds: a write that takes real time (the store's) is under way."""
    for _ in range(2000):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the condition did not come")


async def quiet(n: int = 5) -> None:
    """Let the hop chain and what it started finish: more than a chain's budget, a few times."""
    for _ in range(n):
        await hops(_session.URGENT_HOPS + 5)
        await asyncio.sleep(0.01)


async def attempt(body: Callable[[Any, Counting], Awaitable[Any]], *, view: bool = False) -> Counting:
    """Run ``body(ctx, host)`` as a handler of an object with a table ``t``, over a host that counts its syncs."""
    obj = sessions.SessionObject("riding", migrations=["CREATE TABLE t (x TEXT)"])
    host = Counting(Backend(), service="riding")

    if view:

        @obj.view
        async def look(ctx: sessions.ViewContext) -> None:
            await body(ctx, host)

        await internal.invoke(obj, "look", Host(host.backend, service="riding", shared=True), request())
        return host

    @obj.handler
    async def run(ctx: sessions.SessionContext) -> None:
        await ctx.db.execute("SELECT 1")
        host.syncs = 0
        await body(ctx, host)

    await internal.invoke(obj, "run", host, request())
    return host


async def test_a_rider_is_written_by_the_next_transaction_that_commits_in_it():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        assert ride_commit(ctx.db, "a", insert("rode")) is True
        assert await rows(ctx.db) == []
        assert host.syncs == 0
        async with ctx.db.transaction() as tx:
            await tx.execute("INSERT INTO t VALUES ('own')")
        assert host.syncs == 1
        assert await rows(ctx.db) == ["own", "rode"]
        # Once: the next transaction does not write it again.
        async with ctx.db.transaction() as tx:
            await tx.execute("INSERT INTO t VALUES ('again')")
        assert await rows(ctx.db) == ["again", "own", "rode"]

    await attempt(body)


async def test_a_rider_is_written_by_a_write_outside_a_transaction_in_its_commit():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        ride_commit(ctx.db, "a", insert("rode"))
        await ctx.db.execute("INSERT INTO t VALUES ('own')")
        assert host.syncs == 1
        assert await rows(ctx.db) == ["own", "rode"]

    await attempt(body)


async def test_a_rider_is_not_written_by_a_read_which_commits_nothing():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        ride_commit(ctx.db, "a", insert("rode"))
        await ctx.db.get("SELECT 1")
        await ctx.db.values("SELECT x FROM t")
        await ctx.db.query("SELECT x FROM t")
        assert await rows(ctx.db) == []
        assert host.syncs == 0

    await attempt(body)


async def test_a_rider_is_written_by_the_transaction_that_was_open_when_it_was_registered():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        async with ctx.db.transaction() as tx:
            await tx.execute("INSERT INTO t VALUES ('own')")
            ride_commit(ctx.db, "a", insert("rode"))
            assert await tx.values("SELECT x FROM t") == [["own"]]
        assert host.syncs == 1
        assert await rows(ctx.db) == ["own", "rode"]

    await attempt(body)


async def test_a_rider_stays_pending_when_its_transaction_rolls_back_and_the_next_commit_writes_it():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        ride_commit(ctx.db, "a", insert("rode"))
        with pytest.raises(RuntimeError, match="rolled back"):
            async with ctx.db.transaction() as tx:
                await tx.execute("INSERT INTO t VALUES ('lost')")
                raise RuntimeError("rolled back")
        assert await rows(ctx.db) == []
        await ctx.db.execute("INSERT INTO t VALUES ('own')")
        assert await rows(ctx.db) == ["own", "rode"]

    await attempt(body)


async def test_a_rider_stays_pending_when_its_own_write_fails_and_the_transaction_with_it():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        async def nowhere(tx: Any) -> None:
            await tx.execute("INSERT INTO nowhere VALUES (1)")

        ride_commit(ctx.db, "a", nowhere)
        with pytest.raises(Exception, match="nowhere"):
            await ctx.db.execute("INSERT INTO t VALUES ('own')")
        assert await rows(ctx.db) == []
        # Replaced, it rides the next.
        ride_commit(ctx.db, "a", insert("rode"))
        await ctx.db.execute("INSERT INTO t VALUES ('own')")
        assert await rows(ctx.db) == ["own", "rode"]

    await attempt(body)


async def test_a_rider_is_one_per_slot_a_later_one_replaces_the_earlier_and_none_clears_it():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        ride_commit(ctx.db, "a", insert("first"))
        ride_commit(ctx.db, "a", insert("second"))
        ride_commit(ctx.db, "b", insert("other"))
        ride_commit(ctx.db, "c", insert("cleared"))
        ride_commit(ctx.db, "c", None)
        await ctx.db.execute("INSERT INTO t VALUES ('own')")
        assert await rows(ctx.db) == ["other", "own", "second"]

    await attempt(body)


async def test_a_rider_registered_while_the_commit_that_writes_the_earlier_one_is_under_way_is_for_the_next():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        async def first(tx: Any) -> None:
            await tx.execute("INSERT INTO t VALUES ('first')")
            ride_commit(ctx.db, "a", insert("second"))

        ride_commit(ctx.db, "a", first)
        await ctx.db.execute("INSERT INTO t VALUES ('own')")
        assert await rows(ctx.db) == ["first", "own"]
        await ctx.db.execute("INSERT INTO t VALUES ('next')")
        assert await rows(ctx.db) == ["first", "next", "own", "second"]

    await attempt(body)


async def test_flush_writes_what_is_pending_in_a_transaction_of_its_own_and_nothing_when_nothing_is():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        await flush_riders(ctx.db)
        assert host.syncs == 0
        ride_commit(ctx.db, "a", insert("rode"))
        await flush_riders(ctx.db)
        assert host.syncs == 1
        assert await rows(ctx.db) == ["rode"]
        await flush_riders(ctx.db)
        assert host.syncs == 1

    await attempt(body)


async def test_a_view_cannot_ride_and_a_database_that_is_not_an_attempts_either():
    async def body(ctx: Any, host: Counting) -> None:
        assert ride_commit(ctx.db, "a", insert("rode")) is False
        await flush_riders(ctx.db)
        assert ride_commit(typing.cast(Any, object()), "a", insert("rode")) is False

    await attempt(body, view=True)


async def test_a_pending_rider_is_dropped_with_the_attempt_and_nothing_rides_after_it_ended():
    registered: list[Any] = []

    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        ride_commit(ctx.db, "a", insert("rode"), urgent=True)
        registered.append(ctx.db)

    host = await attempt(body)
    await quiet()
    assert host.syncs == 0
    assert ride_commit(registered[0], "a", insert("late")) is False


async def test_a_database_of_another_name_has_riders_of_its_own():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        other = internal.database(ctx.db, "other")
        await other.execute("CREATE TABLE t (x TEXT)")
        ride_commit(other, "a", insert("rode"))
        await ctx.db.execute("INSERT INTO t VALUES ('own')")
        assert await rows(ctx.db) == ["own"]
        assert await rows(other) == []
        await other.execute("INSERT INTO t VALUES ('own')")
        assert await rows(other) == ["own", "rode"]

    await attempt(body)


# --- Urgent riders: written alone once nothing has started a commit for URGENT_HOPS event-loop iterations ------


async def test_an_urgent_rider_is_written_alone_after_the_hops_with_nothing_under_way_one_commit():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        ride_commit(ctx.db, "a", insert("rode"), urgent=True)
        await hops(_session.URGENT_HOPS // 2)
        assert host.syncs == 0
        await until(lambda: host.syncs > 0)
        await quiet()
        assert host.syncs == 1
        assert await rows(ctx.db) == ["rode"]

    await attempt(body)


async def test_an_urgent_rider_is_written_by_a_transaction_that_starts_within_the_budget_and_not_again():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        ride_commit(ctx.db, "a", insert("rode"), urgent=True)
        await hops(_session.URGENT_HOPS // 2)
        await ctx.db.execute("INSERT INTO t VALUES ('own')")
        assert host.syncs == 1
        await quiet()
        assert host.syncs == 1
        assert await rows(ctx.db) == ["own", "rode"]

    await attempt(body)


async def test_an_urgent_rider_is_written_alone_after_a_rollback_left_it_pending():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        ride_commit(ctx.db, "a", insert("rode"), urgent=True)
        with pytest.raises(RuntimeError):
            async with ctx.db.transaction() as tx:
                await tx.execute("INSERT INTO t VALUES ('lost')")
                raise RuntimeError("rolled back")
        assert host.syncs == 0
        await until(lambda: host.syncs > 0)
        await quiet()
        assert host.syncs == 1
        assert await rows(ctx.db) == ["rode"]

    await attempt(body)


async def test_an_urgent_rider_waits_for_a_transaction_that_is_held_open_longer_than_the_budget():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        async with ctx.db.transaction() as tx:
            await tx.execute("INSERT INTO t VALUES ('own')")
            ride_commit(ctx.db, "a", insert("rode"), urgent=True)
            await hops(_session.URGENT_HOPS * 3)
            assert host.syncs == 0
        assert host.syncs == 1
        await quiet()
        assert host.syncs == 1
        assert await rows(ctx.db) == ["own", "rode"]

    await attempt(body)


async def test_an_urgent_rider_registered_while_the_lone_write_commits_is_written_after_it_in_one_more_commit():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        async def first(tx: Any) -> None:
            await tx.execute("INSERT INTO t VALUES ('first')")
            ride_commit(ctx.db, "b", insert("other"), urgent=True)
            ride_commit(ctx.db, "a", insert("second"), urgent=True)

        ride_commit(ctx.db, "a", first, urgent=True)
        await until(lambda: host.syncs > 0)
        await quiet()
        assert host.syncs == 2
        assert await rows(ctx.db) == ["first", "other", "second"]

    await attempt(body)


async def test_a_lone_write_that_fails_leaves_the_rider_pending_raises_nothing_and_is_not_retried_by_itself():
    unhandled: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    tried: list[int] = []

    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        async def broken(tx: Any) -> None:
            tried.append(1)
            await tx.execute("INSERT INTO nowhere VALUES (1)")

        ride_commit(ctx.db, "a", broken, urgent=True)
        await until(lambda: bool(tried))
        await quiet()
        gc.collect()
        assert tried == [1]
        assert host.syncs == 0
        # Still pending: replaced, it rides the next commit.
        ride_commit(ctx.db, "a", insert("rode"))
        await ctx.db.execute("INSERT INTO t VALUES ('own')")
        assert await rows(ctx.db) == ["own", "rode"]

    try:
        await attempt(body)
        gc.collect()
    finally:
        loop.set_exception_handler(None)
    assert unhandled == []


async def test_a_rider_that_is_not_urgent_is_never_written_by_the_hops():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        ride_commit(ctx.db, "a", insert("rode"))
        await quiet()
        assert host.syncs == 0
        assert await rows(ctx.db) == []

    await attempt(body)


async def test_a_rider_that_replaces_an_urgent_one_stays_urgent():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        ride_commit(ctx.db, "a", insert("first"), urgent=True)
        ride_commit(ctx.db, "a", insert("second"))
        await until(lambda: host.syncs > 0)
        await quiet()
        assert host.syncs == 1
        assert await rows(ctx.db) == ["second"]

    await attempt(body)


async def test_the_lone_write_is_not_refused_by_a_cancellation_that_is_owed():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        owed = [True]
        # What `asked` takes: the handler's next call is the one that fails with it.
        setattr(host, "take_cancellation", lambda: owed.pop() if owed else False)
        ride_commit(ctx.db, "a", insert("rode"), urgent=True)
        await until(lambda: host.syncs > 0)
        await quiet()
        assert host.syncs == 1
        assert owed == [True]

    await attempt(body)


# --- A flush waits for the transactions that are under way ---------------------------------------------------


async def test_a_flush_while_a_transaction_is_under_way_returns_after_the_commit_that_carried_the_rider():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def held() -> None:
            async with ctx.db.transaction() as tx:
                await tx.execute("INSERT INTO t VALUES ('own')")
                entered.set()
                await release.wait()

        under_way = asyncio.ensure_future(held())
        await entered.wait()
        ride_commit(ctx.db, "a", insert("rode"))
        flush = asyncio.ensure_future(flush_riders(ctx.db))
        await hops(30)
        assert not flush.done()
        release.set()
        await under_way
        await flush
        # Written once, by that transaction's commit; the flush had nothing left to commit.
        assert host.syncs == 1
        assert await rows(ctx.db) == ["own", "rode"]

    await attempt(body)


async def test_a_flush_while_a_transaction_is_under_way_that_rolls_back_writes_the_rider_itself():
    async def body(ctx: sessions.SessionContext, host: Counting) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def held() -> None:
            async with ctx.db.transaction() as tx:
                await tx.execute("INSERT INTO t VALUES ('lost')")
                entered.set()
                await release.wait()
                raise RuntimeError("rolled back")

        under_way = asyncio.ensure_future(held())
        await entered.wait()
        ride_commit(ctx.db, "a", insert("rode"))
        flush = asyncio.ensure_future(flush_riders(ctx.db))
        await hops(30)
        assert not flush.done()
        release.set()
        with pytest.raises(RuntimeError, match="rolled back"):
            await under_way
        await flush
        assert host.syncs == 1
        assert await rows(ctx.db) == ["rode"]

    await attempt(body)
