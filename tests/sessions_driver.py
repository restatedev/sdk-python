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
Every cancel signal goes in at a point the test chooses, and what follows it
only once the attempt has acted on it: no test depends on how long anything takes.
"""

# pylint: disable=missing-function-docstring

import asyncio
from datetime import timedelta
import random
import typing
from collections.abc import Awaitable, Callable, Iterator
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


class Gate:
    """Where a handler waits for the test: ``wait`` tells the test the handler is there, and returns once it opens."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.opened = asyncio.Event()
        self.seen: list[str] = []
        """What a handler reports from where it cannot return it: its cleanup, say."""

    async def wait(self) -> None:
        self.reached.set()
        await self.opened.wait()


_gates: list[Gate] = []


@pytest.fixture
def gate() -> Iterator[Gate]:
    """The test's ``Gate``, which its handler reaches with ``gate_of()``."""
    made = Gate()
    _gates.append(made)
    yield made
    _gates.remove(made)


def gate_of() -> Gate:
    return _gates[-1]


async def cancel_at(gate_: Gate, side: RestateSide) -> None:
    """Once the handler waits at ``gate_``, Restate cancels the invocation; once the attempt has acted on that, the
    handler goes on. Nothing the handler awaits is pending, so the cancellation is carried."""
    await gate_.reached.wait()
    await side.cancel()
    gate_.opened.set()


async def run_cancelling_at(gate_: Gate, handler_name: str, side: RestateSide) -> None:
    """``run`` the handler, with Restate cancelling the invocation as the handler reaches ``gate_``."""
    cancelling = asyncio.ensure_future(cancel_at(gate_, side))
    try:
        await run(notes, handler_name, side)
    finally:
        cancelling.cancel()
    assert side.signalled_at is not None, "the handler never reached the gate"


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


async def ended_with(step: Callable[[], Awaitable[Any]]) -> str:
    """As ``said``, and the attempt's end too, which is not an ``Exception``."""
    try:
        return await said(step)
    except BaseException as error:  # pylint: disable=broad-exception-caught
        return type(error).__name__


def call(ctx: sessions.SessionContext, key: str = "a") -> Awaitable[Any]:
    """A call of Restate, which the harness answers with its input, ``"A"``; never, to key ``k``."""
    return ctx.generic_call("Callee", "echo", "A", key=key)


notes = sessions.SessionObject("Notes", migrations=["CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)"])


def insert(ctx: sessions.SessionContext, body: str = "x") -> Callable[[], Awaitable[Any]]:
    """A call of the database: one statement, which commits."""
    return lambda: ctx.db.execute("INSERT INTO notes (body) VALUES (?)", [body])


@notes.handler()
async def add(ctx: sessions.SessionContext, body: str) -> int:
    await ctx.db.execute("INSERT INTO notes (body) VALUES (?)", [body])
    row = await ctx.db.get("SELECT count(*) AS n FROM notes")
    assert row is not None
    return typing.cast(int, row["n"])


@notes.handler()
async def commits(ctx: sessions.SessionContext) -> str:
    """Commits, and says what its failure was if it did not."""
    outcome = await said(insert(ctx))
    return "committed" if outcome == "ok" else outcome


@notes.handler()
async def works_alone(ctx: sessions.SessionContext) -> str:
    """Works outside Restate, with nothing of Restate's outstanding, until the test lets it go on."""
    await gate_of().wait()
    return "done"


@notes.handler()
async def works_beside_a_call(ctx: sessions.SessionContext, annotated: bool) -> str:
    """Waits for a call that is never answered, working meanwhile, inside an annotation or not."""
    pending = asyncio.ensure_future(ctx.generic_call("Callee", "echo", "A", key="k"))
    annotation = internal.annotate(ctx, "tool") if annotated else None
    await gate_of().wait()
    if annotation is not None:
        annotation.end()
    pending.cancel()
    return "done"


@notes.handler()
async def commit_then_reads(ctx: sessions.SessionContext) -> str:
    """A commit, a wait outside Restate where the test cancels the invocation, then two reads: of pages the commit left
    in the session, so they ask Restate nothing."""
    commit = await said(insert(ctx, "a"))
    await gate_of().wait()

    def read() -> Awaitable[Any]:
        return ctx.db.get("SELECT count(*) AS n FROM notes")

    return f"{commit},{await said(read)},{await said(read)}"


@notes.handler()
async def commit_then_call(ctx: sessions.SessionContext) -> str:
    """A commit, then a call: which of the two a cancellation failed."""
    commit = await said(insert(ctx))
    return f"{commit},{await said(lambda: call(ctx))}"


@notes.handler()
async def twice(ctx: sessions.SessionContext) -> str:
    """Two commits, each reporting whether it failed and how."""
    return f"{await said(insert(ctx))},{await said(insert(ctx))}"


@notes.handler()
async def three_times(ctx: sessions.SessionContext) -> str:
    """Three commits, each reporting whether it failed and how."""
    return f"{await said(insert(ctx))},{await said(insert(ctx))},{await said(insert(ctx))}"


@notes.handler()
async def two_commits(ctx: sessions.SessionContext) -> str:
    """A commit, a wait outside Restate where the test cancels the invocation, then a transaction with a send of its
    own: sends are made between a commit's head write and the wait for its ack."""
    first = await said(insert(ctx, "a"))
    await gate_of().wait()

    async def transaction() -> None:
        async with ctx.db.transaction() as tx:
            gate_of().seen.append("body")
            await tx.execute("INSERT INTO notes (body) VALUES (?)", ["b"])
            tx.generic_send("Shipping", "ship", "b", key="b")

    return f"{first},{await said(transaction)}"


@notes.handler()
async def sending_transaction_then_insert(ctx: sessions.SessionContext) -> str:
    """A transaction with a send, whose commit empties the outbox after it, then a commit."""

    async def transaction() -> None:
        async with ctx.db.transaction() as tx:
            await tx.execute("INSERT INTO notes (body) VALUES (?)", ["t"])
            tx.generic_send("Shipping", "ship", "t", key="t")

    return f"{await said(transaction)},{await said(insert(ctx))}"


AUDIT = internal.Schema("audit", ["CREATE TABLE audit (what TEXT NOT NULL)"])


@notes.handler()
async def ensures_twice(ctx: sessions.SessionContext) -> str:
    """A library's schema, ensured twice: applying it is a call of the database the handler asked for."""
    return f"{await said(lambda: AUDIT.ensure(ctx.db))},{await said(lambda: AUDIT.ensure(ctx.db))}"


@notes.handler()
async def call_twice(ctx: sessions.SessionContext) -> str:
    """Two calls of Restate: a carried cancellation has to reach the call's result, not its invocation id."""

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
    ctx.generic_send("Callee", "echo", "S", key="a")
    try:
        return str(await call(ctx))
    except TerminalError as error:
        return "cancelled" if error.status_code == 409 and error.message == "cancelled" else type(error).__name__


@notes.handler()
async def call_then_twice(ctx: sessions.SessionContext) -> str:
    """A call Restate never answers, then two commits."""
    return f"{await said(lambda: call(ctx, 'k'))},{await said(insert(ctx))},{await said(insert(ctx))}"


@notes.handler()
async def unanswered_call(ctx: sessions.SessionContext) -> str:
    """A call Restate never answers."""
    return await said(lambda: call(ctx, "k"))


@notes.handler(explicit_cancellation=True)
async def explicitly(ctx: sessions.SessionContext) -> str:
    """A commit, a call and a commit, in a handler that takes cancellations itself."""
    return f"{await said(insert(ctx))},{await said(lambda: call(ctx))},{await said(insert(ctx))}"


@notes.handler()
async def gathers(ctx: sessions.SessionContext) -> str:
    """A call and a commit at once, then a commit."""
    both = await asyncio.gather(said(lambda: call(ctx)), said(insert(ctx)))
    return f"{','.join(both)},{await said(insert(ctx))}"


@notes.view()
async def counts(ctx: sessions.ViewContext) -> str:
    """Two reads of a view."""

    def read() -> Awaitable[Any]:
        return ctx.db.get("SELECT count(*) AS n FROM notes")

    return f"{await said(read)},{await said(read)}"


@notes.handler()
async def other_database(ctx: sessions.SessionContext) -> str:
    """Two calls of another database of the key, then one of the object's own."""
    other = internal.database(ctx.db, "other")

    def create() -> Awaitable[Any]:
        return other.execute("CREATE TABLE IF NOT EXISTS t (x)")

    return f"{await said(create)},{await said(create)},{await said(insert(ctx))}"


BULK_ROWS = 40


def bulk_rows(seed: int) -> list[str]:
    """About 160 KiB of rows that do not compress, the same for each ``seed``: enough to make a commit checkpoint."""
    rows = random.Random(seed)
    return [rows.randbytes(2000).hex() for _ in range(BULK_ROWS)]


@notes.handler()
async def bulks_then_twice(ctx: sessions.SessionContext) -> str:
    """Three bulk commits, then two commits.

    The third bulk commit checkpoints and then lists the state's keys, to delete the packs no checkpoint needs any
    more: where that falls follows from the shared core's checkpoint thresholds, which the test checks.
    """
    statement = f"INSERT INTO notes (body) VALUES {', '.join(['(?)'] * BULK_ROWS)}"

    def bulk(seed: int) -> Callable[[], Awaitable[Any]]:
        return lambda: ctx.db.execute(statement, bulk_rows(seed))

    bulks = [await said(bulk(1)), await said(bulk(2)), await said(bulk(3))]
    return f"{','.join(bulks)},{await said(insert(ctx))},{await said(insert(ctx))}"


@notes.handler()
async def cleans_up_after_its_end(ctx: sessions.SessionContext) -> str:
    """A commit, then a wait outside Restate that the attempt's end stops; the cleanup calls the database and Restate,
    and reports how each ended."""
    await said(insert(ctx, "a"))
    seen = gate_of().seen
    try:
        await gate_of().wait()
    except asyncio.CancelledError:
        seen.append(await ended_with(insert(ctx, "b")))
        seen.append(await ended_with(lambda: call(ctx)))
        raise
    return "not ended"


def acked(side: RestateSide) -> int:
    """How many of the attempt's commands asked for an ack: a commit each, on either store."""
    return sum(1 for m in side.sent() if 0x0400 <= m.type < 0x8000 and m.flags & REQUESTED_ACK)


def describe(side: RestateSide) -> str:
    """What the attempt wrote, as message types in order: for a failure's message."""
    return " ".join(f"{m.type:#06x}" for m in side.sent())


def acked_after_signal(side: RestateSide) -> int:
    """How many of the commands the attempt wrote after the cancel signal went in asked for an ack: its commits."""
    assert side.signalled_at is not None, f"no cancel signal went in: {describe(side)}"
    return sum(1 for m in side.sent()[side.signalled_at :] if 0x0400 <= m.type < 0x8000 and m.flags & REQUESTED_ACK)


def types_after_signal(side: RestateSide) -> list[int]:
    """The types of what the attempt wrote after the cancel signal went in."""
    assert side.signalled_at is not None, f"no cancel signal went in: {describe(side)}"
    return [m.type for m in side.sent()[side.signalled_at :]]


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


async def test_reads_the_state_beyond_the_head_on_the_wire_and_reads_back_what_it_wrote(store: str):
    # Restate preloads the head alone; the rest of the state is read on the wire. A key this process never opened
    # has no pages cached, so the read goes to Restate.
    side = RestateSide(await seeded(f"kl-seed-{store}"))
    side.start(f"kl-{store}", "second")
    await run(notes, "add", side)
    assert output_of(side.sent()) == 2, describe(side)
    assert side.reads > 0, describe(side)


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


async def test_the_outbox_emptied_after_a_commit_does_not_take_the_cancellation_that_landed_on_the_commit():
    # The signal lands on the transaction's commit, which made its send. The session then empties the outbox, a
    # call of the database of its own: the cancellation is not its, and the transaction reports what it did.
    side = RestateSide(await seeded("kb"), cancel_on_commit=True)
    side.start("kb", None)
    await run(notes, "sending_transaction_then_insert", side)
    sent = side.sent()
    assert output_of(sent) == "ok,cancelled", describe(side)
    # The premise: the signal went in, and the transaction's send went out.
    assert side.signalled_at is not None, describe(side)
    assert TYPE.one_way_call in [m.type for m in sent], describe(side)


async def test_a_cancellation_that_fails_a_call_it_finds_waiting_is_not_carried_as_well():
    # The signal arrives with the call waiting for its result: the call fails, and that was the cancellation. The
    # calls of the database after it succeed.
    side = RestateSide(cancel_on_call="after_id")
    side.start("ka", None)
    await run(notes, "call_then_twice", side)
    assert output_of(side.sent()) == "cancelled,ok,ok", describe(side)
    assert TYPE.cancel_invocation in types_after_signal(side), describe(side)


async def test_a_handler_with_explicit_cancellation_gets_no_carried_cancellation():
    side = RestateSide(cancel_at_start=True)
    side.start("kx", None)
    await run(notes, "explicitly", side)
    assert output_of(side.sent()) == "ok,ok,ok", describe(side)


async def test_a_cancellation_does_not_fail_the_read_of_the_state_it_finds_waiting(store: str):
    # The signal arrives while the session's read of the state waits for Restate (on the VM's store, in Rust):
    # the read is the open's, which the handler's first call waits for. The read and the call run to their end; the
    # cancellation goes to the next call.
    side = RestateSide(await seeded(f"kd-seed-{store}"), cancel_on_read=1)
    side.start(f"kd-{store}", None)
    await run(notes, "three_times", side)
    assert output_of(side.sent()) == "ok,cancelled,ok", describe(side)
    assert side.signalled_at is not None, f"no read went to Restate: {describe(side)}"


async def test_a_cancellation_does_not_fail_the_listing_of_the_state_keys_it_finds_waiting(store: str):
    # The third bulk commit lists the state's keys after it checkpoints. The signal arrives while the listing waits
    # for Restate (on the VM's store, in Rust): the call runs to its end, and the next call gets the cancellation.
    side = RestateSide(cancel_on_keys=True)
    side.start(f"kk-{store}", None)
    await run(notes, "bulks_then_twice", side)
    # The premise: the keys were listed after the third bulk commit, with the open's commit of the schema, four
    # commits in all, before the listing.
    assert side.signalled_at is not None, f"the state's keys were never listed: {describe(side)}"
    assert acked(side) - acked_after_signal(side) == 4, f"the listing came after another commit: {describe(side)}"
    assert output_of(side.sent()) == "ok,ok,ok,cancelled,ok", describe(side)
    # The listing ran to its end, and the session went on: after the signal, the one commit is the last call's.
    assert acked_after_signal(side) == 1, describe(side)


async def test_a_call_whose_invocation_id_comes_after_the_cancellation_is_cancelled_once_it_comes():
    # The signal arrives before the call's invocation id: the call is cancelled as the id comes, before the handler
    # ends. The cancel command names the id, so it went out after the id came.
    side = RestateSide(cancel_on_call="before_id")
    side.start("ki", None)
    await run(notes, "unanswered_call", side)
    sent = side.sent()
    assert output_of(sent) == "cancelled", describe(side)
    types = [m.type for m in sent]
    assert TYPE.cancel_invocation in types, describe(side)
    cancel = types.index(TYPE.cancel_invocation)
    assert cancel < types.index(TYPE.output), describe(side)
    assert sent[cancel].bytes_of(1) == b"inv_started", describe(side)


async def test_a_call_whose_invocation_id_came_before_the_cancellation_is_cancelled():
    side = RestateSide(cancel_on_call="after_id")
    side.start("kj", None)
    await run(notes, "unanswered_call", side)
    sent = side.sent()
    assert output_of(sent) == "cancelled", describe(side)
    types = [m.type for m in sent]
    assert TYPE.cancel_invocation in types, describe(side)
    cancel = types.index(TYPE.cancel_invocation)
    assert cancel < types.index(TYPE.output), describe(side)
    assert sent[cancel].bytes_of(1) == b"inv_started", describe(side)


async def test_once_the_attempt_has_ended_a_carried_cancellation_is_not_delivered():
    # The signal arrives during the handler's local work, and Restate shuts the attempt down. The handler's cleanup
    # calls its database and Restate: each fails with the attempt's end, as it does without the signal, and not with
    # the cancellation, which the next attempt gets.
    async def cleanup_saw(cancelled: bool, key: str) -> list[str]:
        made = Gate()
        _gates.append(made)
        try:
            side = RestateSide()
            side.start(key, None)

            async def end() -> None:
                await made.reached.wait()
                if cancelled:
                    await side.cancel()
                side.shut_down()

            ending = asyncio.ensure_future(end())
            try:
                await run(notes, "cleans_up_after_its_end", side)
            finally:
                ending.cancel()
            assert not cancelled or side.signalled_at is not None, describe(side)
            return made.seen
        finally:
            _gates.remove(made)

    without = await cleanup_saw(False, "kf1")
    with_signal = await cleanup_saw(True, "kf2")
    assert len(without) == 2 and without[1] == "SuspendedException", without
    assert with_signal == without, f"with the signal: {with_signal}; without: {without}"


async def test_a_carried_cancellation_goes_to_the_first_of_a_call_and_a_commit_made_at_once():
    # ``gather`` starts its steps in order: the call is made first, and takes the carry as it makes its result's
    # future; the commit after it runs.
    side = RestateSide(cancel_at_start=True)
    side.start("kg", None)
    await run(notes, "gathers", side)
    assert output_of(side.sent()) == "cancelled,ok,ok", describe(side)


async def test_a_view_takes_a_carried_cancellation_at_its_first_read():
    side = RestateSide(await seeded("kv"), cancel_at_start=True)
    side.start("kv", None)
    await run(notes, "counts", side)
    assert output_of(side.sent()) == "cancelled,ok", describe(side)


async def test_another_database_of_the_key_takes_a_carried_cancellation_as_the_objects_own_does():
    side = RestateSide(cancel_at_start=True)
    side.start("ko", None)
    await run(notes, "other_database", side)
    assert output_of(side.sent()) == "cancelled,ok,ok", describe(side)


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


def heartbeats(side: RestateSide) -> int:
    """How many times the attempt told Restate that it makes progress."""
    return sum(1 for m in side.sent() if m.type == TYPE.awaiting_on)


async def beats_while_at(gate_: Gate, handler: str, side: RestateSide) -> int:
    """Run ``handler`` until it waits at ``gate_``, leave it there for ten heartbeat intervals, then let it end.

    Returns the heartbeats it sent while it waited there.
    """
    running = asyncio.ensure_future(run(notes, handler, side))
    await gate_.reached.wait()
    before = heartbeats(side)
    await asyncio.sleep(10 * _driver.HEARTBEAT_INTERVAL.total_seconds())
    during = heartbeats(side) - before
    gate_.opened.set()
    await running
    return during


@pytest.fixture
def quick_heartbeats(monkeypatch) -> None:
    monkeypatch.setattr(_driver, "HEARTBEAT_INTERVAL", timedelta(seconds=0.02))


async def test_an_attempt_heartbeats_while_its_handler_works_with_nothing_of_restates_outstanding(
    gate: Gate, quick_heartbeats: None
):
    # A model request, say: Restate hears nothing from the attempt meanwhile, and shuts down one it heard nothing
    # from for its inactivity timeout. The next attempt would make the request again, and never get past it.
    side = RestateSide()
    side.start("kh1", None)
    assert await beats_while_at(gate, "works_alone", side) >= 3, describe(side)
    assert output_of(side.sent()) == "done", describe(side)


async def test_an_attempt_does_not_heartbeat_while_an_answer_of_restates_is_outstanding(
    gate: Gate, quick_heartbeats: None
):
    # A wait of days for an approval holds no process: Restate shuts the attempt down, and starts the next one when
    # the answer comes.
    side = RestateSide()
    side.start("kh2", False)
    assert await beats_while_at(gate, "works_beside_a_call", side) == 0, describe(side)


async def test_an_open_annotation_heartbeats_beside_an_outstanding_answer(gate: Gate, quick_heartbeats: None):
    # The annotation says the handler is working, on a tool beside the call it waits for.
    side = RestateSide()
    side.start("kh3", True)
    assert await beats_while_at(gate, "works_beside_a_call", side) >= 3, describe(side)


async def test_a_read_that_asks_restate_nothing_sees_a_carried_cancellation_too_once(gate: Gate):
    # A read of pages the session holds is answered without a wait on Restate, so a cancellation delivered at the
    # store's waits had nothing to land on. It is delivered where the handler calls.
    side = RestateSide()
    side.start("kr", None)
    await run_cancelling_at(gate, "commit_then_reads", side)
    assert output_of(side.sent()) == "ok,cancelled,ok", describe(side)
    # The premise: the reads after the signal asked Restate for nothing.
    assert types_after_signal(side) == [TYPE.output, TYPE.end], describe(side)


async def test_a_carried_cancellation_fails_the_next_call_of_the_database_before_it_writes_or_sends_anything(
    gate: Gate,
):
    # A call that fails with the cancellation did nothing: the cancellation is delivered where the handler calls,
    # before the call issues a command.
    side = RestateSide()
    side.start("kc", None)
    await run_cancelling_at(gate, "two_commits", side)
    sent = side.sent()
    assert output_of(sent) == "ok,cancelled", describe(side)
    # The transaction failed before its body ran.
    assert not gate.seen, gate.seen
    # What came after the signal went in: the handler's output, and before it no command at all.
    after = [t for t in types_after_signal(side) if 0x0400 <= t < 0x8000]
    assert after == [TYPE.output], f"the cancelled transaction issued commands: {describe(side)}"
    # Its sends are made between a commit's head write and the wait for that write's ack, so they are the first
    # thing to escape a commit that is failed late.
    assert not any(m.type == TYPE.one_way_call for m in sent), "the cancelled transaction's sends went out"


async def test_a_retrys_carried_cancellation_fails_the_calls_result_not_the_id_it_makes_first():
    # A call makes the invocation id's future before the result's. A carry the id took would let the call run to
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
