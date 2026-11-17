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
"""A session object's attempt on the explicit recovery VM, with Restate's side played on the wire (``sessions_wire``).

No server and no endpoint: the attempt runs through the object's runner, and a
real database over the object's state. Above all, where a cancellation lands.
"""

# pylint: disable=missing-function-docstring

import asyncio
import math
import typing
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from restate import TerminalError, sessions
from restate.sessions import _driver, internal
from restate.sessions._object import HEAD_KEY
from sessions_wire import REQUESTED_ACK, TYPE, RestateSide, output_of, run  # pylint: disable=import-error


@pytest.fixture
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]


@pytest.fixture(autouse=True, params=["vm", "python"])
def store(request, monkeypatch):
    """Each test runs on both stores: the VM's, which the extension reads and writes directly, and the Python store
    over the context's state commands, which an endpoint with a journal codec uses."""
    if request.param == "python":
        monkeypatch.setattr(_driver.VmHost, "vm", property(lambda self: None))
    return request.param


async def said(step: Callable[[], Awaitable[Any]]) -> str:
    """How ``step`` ended: ``ok``, ``cancelled``, or the name of what else it failed with."""
    try:
        await step()
        return "ok"
    except TerminalError as error:
        if error.status_code == 409 and error.message == "cancelled":
            return "cancelled"
        return type(error).__name__
    except Exception as error:  # pylint: disable=broad-exception-caught
        return type(error).__name__


def call(ctx: sessions.SessionContext) -> Awaitable[Any]:
    """A call of Restate, which the harness answers with its input: ``"A"``."""
    return ctx.generic_call("Callee", "echo", "A", key="a")


notes = sessions.SessionObject("Notes", migrations=["CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)"])


@notes.handler()
async def add(ctx: sessions.SessionContext, body: str) -> int:
    await ctx.db.execute("INSERT INTO notes (body) VALUES (?)", [body])
    row = await ctx.db.get("SELECT count(*) AS n FROM notes")
    assert row is not None
    return typing.cast(int, row["n"])


@notes.handler()
async def commits(ctx: sessions.SessionContext) -> str:
    """Commits, and says what its failure was if it did not."""
    outcome = await said(lambda: ctx.db.execute("INSERT INTO notes (body) VALUES (?)", ["x"]))
    return "committed" if outcome == "ok" else outcome


@notes.handler()
async def commit_then_reads(ctx: sessions.SessionContext) -> str:
    """A commit, work outside Restate long enough for a cancellation to land with nothing waiting, then two reads:
    of pages the commit left in the session, so they ask Restate nothing."""
    commit = await said(lambda: ctx.db.execute("INSERT INTO notes (body) VALUES (?)", ["a"]))
    await asyncio.sleep(0.3)

    def read() -> Awaitable[Any]:
        return ctx.db.get("SELECT count(*) AS n FROM notes")

    return f"{commit},{await said(read)},{await said(read)}"


@notes.handler()
async def commit_then_call(ctx: sessions.SessionContext) -> str:
    """A commit, then a call: which of the two a cancellation failed."""
    commit = await said(lambda: ctx.db.execute("INSERT INTO notes (body) VALUES (?)", ["x"]))
    return f"{commit},{await said(lambda: call(ctx))}"


@notes.handler()
async def twice(ctx: sessions.SessionContext) -> str:
    """Two commits, each reporting whether it failed and how."""

    def once() -> Awaitable[str]:
        return said(lambda: ctx.db.execute("INSERT INTO notes (body) VALUES (?)", ["x"]))

    return f"{await once()},{await once()}"


@notes.handler()
async def two_commits(ctx: sessions.SessionContext) -> str:
    """A commit, work outside Restate long enough for a cancellation to land with nothing waiting, then a
    transaction with a send of its own: sends are made between a commit's head write and the wait for its ack."""
    first = await said(lambda: ctx.db.execute("INSERT INTO notes (body) VALUES (?)", ["a"]))
    await asyncio.sleep(0.3)

    async def transaction() -> None:
        async with ctx.db.transaction() as tx:
            await tx.execute("INSERT INTO notes (body) VALUES (?)", ["b"])
            tx.generic_send("Shipping", "ship", "b", key="b")

    return f"{first},{await said(transaction)}"


AUDIT = internal.Schema("audit", ["CREATE TABLE audit (what TEXT NOT NULL)"])


@notes.handler()
async def ensures_twice(ctx: sessions.SessionContext) -> str:
    """A library's schema, ensured twice: applying it is a call of the database the handler asked for."""

    def once() -> Awaitable[str]:
        return said(lambda: AUDIT.ensure(ctx.db))

    return f"{await once()},{await once()}"


@notes.handler()
async def call_twice(ctx: sessions.SessionContext) -> str:
    """Two calls of Restate: a carried cancellation has to reach the call's result, not its invocation id."""
    # The signal has to land with nothing in flight, or the first call is failed where it waits and the carry is
    # never exercised.
    await asyncio.sleep(0.05)

    async def once() -> str:
        try:
            return str(await call(ctx))
        except TerminalError as error:
            return "cancelled" if error.status_code == 409 and error.message == "cancelled" else type(error).__name__

    return f"{await once()},{await once()}"


@notes.handler()
async def send_then_call(ctx: sessions.SessionContext) -> str:
    """A send first, which makes an invocation id and nothing else: it must not swallow the carried cancellation
    that the call after it is owed."""
    await asyncio.sleep(0.05)
    ctx.generic_send("Callee", "echo", "S", key="a")
    try:
        return str(await call(ctx))
    except TerminalError as error:
        return "cancelled" if error.status_code == 409 and error.message == "cancelled" else type(error).__name__


class Cancelling:
    """Restate cancels the invocation after ``delay`` seconds; ``sent_at_cancel`` is how much the attempt had
    written by then."""

    def __init__(self, side: RestateSide, delay: float) -> None:
        self.sent_at_cancel: float = math.inf
        self._side = side
        self._timer = asyncio.get_running_loop().call_later(delay, self._cancel)

    def _cancel(self) -> None:
        self.sent_at_cancel = len(self._side.sent())
        self._side.cancel()

    def stop(self) -> None:
        self._timer.cancel()


def acked(side: RestateSide) -> int:
    """How many of the attempt's commands asked for an ack: a commit each, on either store."""
    return sum(1 for m in side.sent() if 0x0400 <= m.type < 0x8000 and m.flags & REQUESTED_ACK)


def describe(side: RestateSide) -> str:
    """What the attempt wrote, as message types in order: for a failure's message."""
    return " ".join(f"{m.type:#06x}" for m in side.sent())


async def seeded(key: str) -> dict[str, bytes]:
    """The state an earlier invocation of ``key`` left: its schema is applied.

    On a key never written, the first commit of an attempt is the schema's, made as the session opens, and it is
    that one ``cancel_on_commit`` would land on. After this, it is the handler's own.
    """
    side = RestateSide()
    side.start(key, "seed")
    await run(notes, "add", side)
    assert output_of(side.sent()) == 1, describe(side)
    return side.state


async def test_runs_a_handler_commits_its_database_and_answers():
    side = RestateSide()
    side.start("k1", "first")
    await run(notes, "add", side)
    assert side.head is not None and side.head["status"] == 200
    assert side.ended
    sent = side.sent()
    assert output_of(sent) == 1
    assert sent[-1].type == TYPE.end
    assert HEAD_KEY in side.state


async def test_a_cancellation_that_lands_on_a_commit_in_flight_is_delivered_once_to_the_await_after_it():
    # One signal, and the handler sees one cancellation: the commit under way runs to its end, and the call after
    # it fails.
    side = RestateSide(await seeded("k6"), cancel_on_commit=True)
    side.start("k6", None)
    await run(notes, "commit_then_call", side)
    assert output_of(side.sent()) == "ok,cancelled", describe(side)
    # The premise: the attempt made one commit, the handler's, so that is the one the signal landed on.
    assert acked(side) == 1, describe(side)


async def test_a_cancellation_does_not_fail_a_commit_already_under_way_its_head_is_out_and_it_lands():
    # The signal arrives while the commit waits for the ack of its head write. The commit runs to its end and says
    # so; the cancellation is owed to the handler's next await, and this handler has none.
    side = RestateSide(await seeded("k5"), cancel_on_commit=True)
    side.start("k5", None)
    await run(notes, "commits", side)
    assert output_of(side.sent()) == "committed", describe(side)
    assert HEAD_KEY in side.state
    # The premise: the attempt made one commit, the handler's, so that is the one the signal landed on.
    assert acked(side) == 1, describe(side)
    # And what it committed is there to read: the seed's row, and this one.
    after = RestateSide(side.state)
    after.start("k5", "third")
    await run(notes, "add", after)
    assert output_of(after.sent()) == 3, describe(after)


async def test_a_retry_of_a_cancelled_invocation_fails_its_first_call_and_works_after_it():
    # The cancel signal is among the earlier attempts' entries, so the VM knows before the handler starts and
    # nothing is in flight to fail: the cancellation is carried to the handler's first call.
    side = RestateSide(cancel_at_start=True)
    side.start("k7", None)
    await run(notes, "twice", side)
    assert output_of(side.sent()) == "cancelled,ok", describe(side)


async def test_a_schema_the_handler_ensures_takes_a_carried_cancellation_as_its_statements_do():
    side = RestateSide(cancel_at_start=True)
    side.start("ke", None)
    await run(notes, "ensures_twice", side)
    assert output_of(side.sent()) == "cancelled,ok", describe(side)


async def test_a_read_that_asks_restate_nothing_sees_a_carried_cancellation_too_once():
    # A read of pages the session holds is answered without a wait on Restate, so a cancellation delivered at the
    # store's waits had nothing to land on. It is delivered where the handler calls.
    side = RestateSide()
    side.start("kr", None)
    cancelling = Cancelling(side, 0.15)
    await run(notes, "commit_then_reads", side)
    cancelling.stop()
    sent = side.sent()
    assert output_of(sent) == "ok,cancelled,ok", describe(side)
    # The premise: the reads after the signal asked Restate for nothing.
    assert cancelling.sent_at_cancel < len(sent), "the signal went in before the handler ended"
    after = sent[int(cancelling.sent_at_cancel) :]
    assert [m.type for m in after] == [TYPE.output, TYPE.end], describe(side)


async def test_a_carried_cancellation_fails_the_next_call_of_the_database_before_it_writes_or_sends_anything(
    store: str,
):
    # A call that fails with the cancellation did nothing: the cancellation is delivered where the handler calls,
    # before the call issues a command.
    side = RestateSide()
    side.start("kc", None)
    cancelling = Cancelling(side, 0.15)
    await run(notes, "two_commits", side)
    cancelling.stop()
    sent = side.sent()
    assert output_of(sent) == "ok,cancelled", describe(side)
    assert cancelling.sent_at_cancel < len(sent), "the signal went in before the handler ended"
    # What came after the signal went in: the handler's output, and before it no command at all.
    after = [m.type for m in sent[int(cancelling.sent_at_cancel) :] if 0x0400 <= m.type < 0x8000]
    assert after == [TYPE.output], f"the cancelled transaction issued commands: {describe(side)}"
    # Its sends are made between a commit's head write and the wait for that write's ack, so they are the first
    # thing to escape a commit that is failed late.
    assert not any(m.type == TYPE.one_way_call for m in sent), "the cancelled transaction's sends went out"
    # Every head write is synced: that ack is the commit's sync. The VM's store writes the head *with* its sync,
    # asking for the ack on the head write itself; the Python store writes it plain and then clears ``v22/sync``
    # with an ack (``VmHost.sync``).
    heads = [i for i, m in enumerate(sent) if m.type == TYPE.set_state and m.bytes_of(1).decode() == HEAD_KEY]
    assert heads, describe(side)
    if store == "vm":
        assert all(sent[i].flags & REQUESTED_ACK for i in heads), describe(side)
    else:
        synced = [
            sent[i + 1].type == TYPE.clear_state
            and sent[i + 1].bytes_of(1).decode() == _driver.SYNC_KEY
            and bool(sent[i + 1].flags & REQUESTED_ACK)
            for i in heads
        ]
        assert all(synced), describe(side)


async def test_a_retrys_carried_cancellation_fails_the_calls_result_not_the_id_it_makes_first():
    # A call makes the invocation id's future before the result's. Handing the carry to the id let the call run to
    # completion and the handler see its result: the cancellation lost.
    side = RestateSide(cancel_at_start=True)
    side.start("k8", None)
    await run(notes, "call_twice", side)
    assert output_of(side.sent()) == "cancelled,A", describe(side)


async def test_a_send_does_not_swallow_the_cancellation_the_call_after_it_is_owed():
    side = RestateSide(cancel_at_start=True)
    side.start("k9", None)
    await run(notes, "send_then_call", side)
    assert output_of(side.sent()) == "cancelled", describe(side)
