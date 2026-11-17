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
"""A transaction under a cancellation, on the wire (``sessions_wire``).

Wherever the signal lands, ``transaction()`` either returns, and then its rows
are committed and each of its sends went out once, or raises the cancellation
(``TerminalError("cancelled", 409)``), and then it committed nothing and sent
nothing. And the handler sees one cancellation for the one signal.
"""

# pylint: disable=missing-function-docstring

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest

from restate import TerminalError, sessions
from restate.sessions import _driver
from sessions_wire import (  # pylint: disable=import-error
    REQUESTED_ACK,
    TYPE,
    Made,
    Message,
    RestateSide,
    outcome,
    run,
)


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


def is_cancellation(error: BaseException) -> bool:
    return isinstance(error, TerminalError) and error.status_code == 409 and error.message == "cancelled"


async def said(step: Callable[[], Awaitable[Any]]) -> str:
    """How ``step`` ended: ``ok``, ``cancelled``, or the name of what else it failed with."""
    try:
        await step()
        return "ok"
    except Exception as error:  # pylint: disable=broad-exception-caught
        return "cancelled" if is_cancellation(error) else type(error).__name__


class Seen:
    """What a handler reports from where it cannot return it, and where it works outside Restate for a test."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        """How each noted step ended, by class name (or ``cancelled``)."""
        self.terminal: list[bool] = []
        """Whether each error ``seen`` has is a ``TerminalError``, which a saga compensates on."""
        self.reached = asyncio.Event()
        self.opened = asyncio.Event()
        self.opened.set()

    def note(self, error: BaseException | None) -> None:
        if error is None:
            self.seen.append("ok")
            return
        self.terminal.append(isinstance(error, TerminalError))
        self.seen.append(
            "cancelled" if is_cancellation(error) else f"{type(error).__module__}.{type(error).__qualname__}"
        )

    def hold(self) -> None:
        self.opened.clear()

    async def work(self) -> None:
        """Work outside Restate, until the test lets it go on."""
        self.reached.set()
        await self.opened.wait()


_seen: list[Seen] = []


@pytest.fixture(autouse=True)
def seen() -> Iterator[Seen]:
    made = Seen()
    _seen.append(made)
    yield made
    _seen.remove(made)


ledger = sessions.SessionObject("Ledger", migrations=["CREATE TABLE rows (id INTEGER PRIMARY KEY, body TEXT)"])


async def insert_and_send(db: sessions.Database, body: str) -> None:
    """One transaction that reads, inserts ``body`` and sends ``body`` to ``Mail/notify``."""
    async with db.transaction() as tx:
        await tx.get("SELECT count(*) AS n FROM rows")
        await tx.execute("INSERT INTO rows (body) VALUES (?)", [body])
        tx.generic_send("Mail", "notify", body, key=body)


def statement(ctx: sessions.SessionContext, body: str) -> Callable[[], Awaitable[Any]]:
    return lambda: ctx.db.execute("INSERT INTO rows (body) VALUES (?)", [body])


def echo(ctx: sessions.SessionContext) -> Awaitable[Any]:
    """The call each handler makes last: ``Callee/echo``."""
    return ctx.generic_call("Callee", "echo", "A", key="a")


@ledger.handler()
async def seed(ctx: sessions.SessionContext) -> str:
    """Opens the database, so that the schema's commit is behind the key."""
    return "seeded"


@ledger.handler()
async def sweep(ctx: sessions.SessionContext) -> str:
    """One transaction, one statement, one call: how each ended."""
    tx = await said(lambda: insert_and_send(ctx.db, "tx"))
    after = await said(statement(ctx, "after"))
    call = await said(lambda: echo(ctx))
    return f"{tx},{after},{call}"


@ledger.handler()
async def sweep_later(ctx: sessions.SessionContext) -> str:
    """A statement, then the sweep's: the transaction is not the first call of the database, so a signal that lands
    during the statement is carried into it."""
    before = await said(statement(ctx, "before"))
    tx = await said(lambda: insert_and_send(ctx.db, "tx"))
    after = await said(statement(ctx, "after"))
    call = await said(lambda: echo(ctx))
    return f"{before},{tx},{after},{call}"


@ledger.handler()
async def body_calls(ctx: sessions.SessionContext, catches: bool) -> str:
    """A transaction whose body awaits a call; it catches that call's failure if ``catches``."""
    in_body = "not reached"

    def work() -> Awaitable[Any]:
        return ctx.generic_call("Slow", "work", "W", key="a")

    async def transaction() -> None:
        nonlocal in_body
        async with ctx.db.transaction() as t:
            await t.execute("INSERT INTO rows (body) VALUES ('tx')")
            called = "ok"
            if catches:
                called = await said(work)
            else:
                await work()
            in_body = called
            t.generic_send("Mail", "notify", "tx", key="tx")

    tx = await said(transaction)
    after = await said(statement(ctx, "after"))
    return f"{in_body},{tx},{after}"


@ledger.handler()
async def two(ctx: sessions.SessionContext) -> str:
    """Two transactions, one after the other, then a statement."""
    first = await said(lambda: insert_and_send(ctx.db, "first"))
    second = await said(lambda: insert_and_send(ctx.db, "second"))
    after = await said(statement(ctx, "after"))
    return f"{first},{second},{after}"


@ledger.handler()
async def noted(ctx: sessions.SessionContext) -> str:
    """The sweep's transaction, noting how it ended where a test can read it."""
    here = _seen[-1]
    try:
        await insert_and_send(ctx.db, "tx")
    except BaseException as error:
        here.note(error)
        # The attempt's end is not the handler's to swallow.
        if not isinstance(error, Exception):
            raise
    else:
        here.note(None)
    return "done"


@ledger.handler()
async def awaits_a_call(ctx: sessions.SessionContext) -> str:
    """A call whose result Restate holds, noting how its await ended where a test can read it."""
    here = _seen[-1]
    try:
        await ctx.generic_call("Slow", "work", "W", key="a")
    except BaseException as error:
        here.note(error)
        raise
    here.note(None)
    return "done"


@ledger.handler()
async def throws(ctx: sessions.SessionContext) -> str:
    """A transaction whose body writes, works outside Restate, and raises."""
    here = _seen[-1]

    async def transaction() -> None:
        async with ctx.db.transaction() as t:
            await t.execute("INSERT INTO rows (body) VALUES ('tx')")
            t.generic_send("Mail", "notify", "tx", key="tx")
            await here.work()
            await t.get("SELECT count(*) AS n FROM rows")
            raise ValueError("the body failed")

    tx = await said(transaction)
    after = await said(statement(ctx, "after"))
    call = await said(lambda: echo(ctx))
    return f"{tx},{after},{call}"


@ledger.view()
async def rows_of(ctx: sessions.ViewContext) -> list[str]:
    return [str(row["body"]) for row in await ctx.db.query("SELECT body FROM rows ORDER BY id")]


# --- Running attempts


def describe(side: RestateSide) -> str:
    """What the attempt wrote, as message types in order (``!`` for a command that asks for an ack)."""
    return " ".join(f"{m.type:#06x}{'!' if m.flags & REQUESTED_ACK else ''}" for m in side.sent())


async def attempt(handler: str, state: dict[str, bytes], key: str, input_: Any = None, **options: Any) -> RestateSide:
    """Run ``handler`` on ``state`` for ``key`` with Restate's side ``options``, answering everything."""
    side = RestateSide(state, id_per_call=True, **options)
    side.start(key, input_)
    await run(ledger, handler, side)
    return side


async def seeded(key: str) -> dict[str, bytes]:
    """The state an earlier invocation of ``key`` left: its schema is applied, so an attempt's commits are its own."""
    side = await attempt("seed", {}, key)
    assert returned(side) == "seeded", describe(side)
    return side.state


async def stored_rows(state: dict[str, bytes], key: str) -> list[str]:
    """The rows stored in ``state``, read by a later attempt."""
    side = RestateSide(state)
    side.start(key, None)
    await run(ledger, "rows_of", side)
    ended = outcome(side.sent())
    assert ended is not None and ended[0] == "value", ended
    return list(ended[1])


def returned(side: RestateSide) -> str:
    """The handler's output, which these handlers end with."""
    ended = outcome(side.sent())
    assert ended is not None and ended[0] == "value", f"the handler ended with {ended}: {describe(side)}"
    return str(ended[1])


def sends_of(side: RestateSide, body: str) -> list[Made]:
    """The sends of ``body`` the attempt made."""
    return [s for s in side.sends if s.input == f'"{body}"']


def is_command(m: Message) -> bool:
    return 0x0400 <= m.type < 0x8000


def acks_asked(sent: list[Message]) -> list[int]:
    """The positions in ``sent`` of the commands that asked for an ack."""
    return [i for i, m in enumerate(sent) if is_command(m) and m.flags & REQUESTED_ACK]


def writes_before_send(side: RestateSide) -> list[int]:
    """The types of the commands the attempt sent before its first send."""
    sent = side.sent()
    first = next(i for i, m in enumerate(sent) if m.type == TYPE.one_way_call)
    return [m.type for m in sent[:first] if is_command(m) and m.type != TYPE.input]


def send_is_inside_commit(sent: list[Message], store: str, commit: int) -> bool:
    """Whether the first send went out between the writes of the ``commit``-th commit that asks for an ack (from 0)
    and Restate's ack of it.

    On the VM's store the commit's last write asks for the ack, and the sends follow it; on the Python store the
    sends follow the writes, and the sync that asks for the ack follows the sends.
    """
    acks = acks_asked(sent)
    send = next(i for i, m in enumerate(sent) if m.type == TYPE.one_way_call)
    if store == "vm":
        return acks[commit] < send < acks[commit + 1]
    before = acks[commit - 1] if commit > 0 else 0
    writes = [i for i in range(before + 1, send) if sent[i].type == TYPE.set_state]
    return bool(writes) and before < send < acks[commit] and sent[acks[commit]].type == TYPE.clear_state


def keyed(name: str, store: str) -> str:
    """The test's key on this store: the page cache is the process's, and every test runs on both stores."""
    return f"{name}-{store}"


# --- The sweep


@dataclass
class Swept:
    """One ``n`` of a sweep: what was outstanding when the signal went in, and what the handler saw."""

    n: int
    outstanding: str
    outcomes: str
    rows: list[str]
    sends: int


def table_of(swept: list[Swept]) -> str:
    lines = [f"{'n':>2}  {'outstanding':<58} {'outcomes':<34} {'rows':<20} sends"]
    for s in swept:
        lines.append(f"{s.n:>2}  {s.outstanding:<58} {s.outcomes:<34} {' '.join(s.rows):<20} {s.sends}")
    return "\n".join(lines)


ACKS = [
    "the transaction's commit (writes out, ack outstanding)",
    "the outbox's flush, after the transaction's commit",
    "the statement's commit, after the transaction",
]
"""What the sweep's acks are of, in order."""

ACKS_LATER = ["the first statement's commit, before the transaction", *ACKS]
"""What ``sweep_later``'s acks are of, in order."""


def phase(side: RestateSide, n: int, acks: list[str]) -> str:
    """What was outstanding when the signal went in at the ``n``-th ask of ``side``."""
    ask = side.asks[n - 1]
    if ask.startswith("read "):
        return "read of the state, by the open the first call waits for"
    if ask != "ack":
        return ask
    ordinal = side.asks[:n].count("ack")
    return acks[ordinal - 1] if ordinal <= len(acks) else "ack"


async def sweep_over(handler: str, store: str, steps: list[str], acks: list[str], commit: int) -> list[Swept]:
    """Run ``handler`` with the signal before each successive answer, checking each run; its table.

    ``steps`` names the handler's outcomes, in order; ``commit`` is the transaction's commit among those that ask
    for an ack (from 0).
    """
    table: list[Swept] = []
    violations: list[str] = []
    reads = 0
    n = 0
    while True:
        n += 1
        assert n <= 200, "the sweep did not end"
        key = keyed(f"{handler}{n}", store)
        # Seeded under another key, so another namespace of the page cache: the attempt starts cold, and reads its
        # pages from Restate.
        state = await seeded(f"{key}-seed")
        r = await attempt(handler, state, key, cancel_before_answer=n)
        # Past the last thing a run asks for, the signal never goes in.
        if r.signalled_at is None:
            break
        sent = r.sent()
        reads += sum(1 for a in r.asks if a.startswith("read "))
        outcomes = returned(r)
        ended = dict(zip(steps, outcomes.split(",")))
        stored = await stored_rows(state, key)
        sends = sends_of(r, "tx")
        table.append(Swept(n, phase(r, n, acks), outcomes, stored, len(sends)))

        problems: list[str] = []
        if any(m.type == TYPE.one_way_call for m in sent) and not send_is_inside_commit(sent, store, commit):
            problems.append("the transaction's send is not inside its commit")
        tx = ended["tx"]
        if tx == "ok":
            if "tx" not in stored:
                problems.append("returned, but its row is not stored")
            if len(sends) != 1 or len({s.idempotency_key for s in sends}) != 1:
                problems.append("returned, but its send did not go out once")
        elif tx == "cancelled":
            if "tx" in stored:
                problems.append("raised the cancellation, but its row is stored")
            if sends:
                problems.append("raised the cancellation, but its send went out")
        else:
            problems.append(f"ended with {tx}")
        for name in ("before", "after"):
            if name in ended and (ended[name] == "ok") != (name in stored):
                problems.append(f"the statement {name!r} said one thing, the rows another")
        cancelled = sum(1 for o in ended.values() if o == "cancelled")
        # The call is the handler's last await: a signal that comes after its answer has nothing left to fail.
        last_ask = len(r.asks) - r.asks[::-1].index("call Callee/echo")
        owed = 1 if n <= last_ask else 0
        if cancelled != owed:
            problems.append(f"{cancelled} cancellations for one signal, {owed} owed")
        violations.extend(
            f"n={n} ({r.asks[n - 1]}): {what}; {outcomes}, rows {stored}, sends {len(sends)}, wire {describe(r)}"
            for what in problems
        )
    shown = table_of(table)
    print(f"\n{handler} on the {store} store:\n{shown}")
    assert not violations, "\n".join(violations) + "\n" + shown
    # The premise: the sweep covered reads that reached Restate, acks and the call's answer.
    assert reads > 0, f"no read reached Restate: the page cache answered them all\n{shown}"
    outstanding = {t.outstanding for t in table}
    assert set(acks) | {"call Callee/echo"} <= outstanding, shown
    return table


async def test_1_a_sweep_of_the_signal_over_every_answer_the_attempt_waits_for(store: str):
    await sweep_over("sweep", store, ["tx", "after", "call"], ACKS, 0)


async def test_1b_the_sweep_with_a_statement_first_so_the_transaction_can_be_refused_by_a_carried_cancellation(
    store: str,
):
    table = await sweep_over("sweep_later", store, ["before", "tx", "after", "call"], ACKS_LATER, 1)
    # The premise: a signal that lands during the first statement's commit is carried into the transaction.
    carried = [t for t in table if t.outstanding == ACKS_LATER[0]]
    assert carried and all(t.outcomes == "ok,cancelled,ok,ok" for t in carried), table_of(table)


# --- A body that awaits a call


async def run_body_calls(store: str, name: str, catches: bool) -> tuple[RestateSide, dict[str, bytes], str]:
    key = keyed(name, store)
    state = await seeded(key)
    r = RestateSide(state, id_per_call=True, hold=["Slow/work"])
    r.start(key, catches)
    running = asyncio.ensure_future(run(ledger, "body_calls", r))
    await r.until(lambda: r.has_called("Slow/work"), "the call in the body")
    await r.cancel()
    await running
    return r, state, key


async def test_2_the_body_awaits_a_call_when_the_signal_comes_the_call_is_cancelled_and_the_transaction_rolls_back(
    store: str,
):
    r, state, key = await run_body_calls(store, "body1", False)
    # The body did not get past its call; the transaction raised.
    assert returned(r) == "not reached,cancelled,ok", describe(r)
    assert await stored_rows(state, key) == ["after"]
    assert r.sends == []
    assert r.cancels == [r.calls[0].invocation_id], describe(r)


async def test_2b_the_body_catches_the_cancellation_and_goes_on_the_transaction_commits_with_its_send(store: str):
    r, state, key = await run_body_calls(store, "body2", True)
    # The body saw the one cancellation, so the transaction and the statement after it work.
    assert returned(r) == "cancelled,ok,ok", describe(r)
    assert await stored_rows(state, key) == ["tx", "after"]
    assert len(sends_of(r, "tx")) == 1
    assert r.cancels == [r.calls[0].invocation_id], describe(r)


# --- A carried cancellation


async def test_3_a_carried_cancellation_refuses_the_transaction_before_it_begins_the_next_one_commits(store: str):
    key = keyed("carried", store)
    state = await seeded(key)
    r = await attempt("two", state, key, cancel_at_start=True)
    assert returned(r) == "cancelled,ok,ok", describe(r)
    assert await stored_rows(state, key) == ["second", "after"]
    assert sends_of(r, "first") == []
    assert len(sends_of(r, "second")) == 1
    # The refused one put nothing on the wire: up to the first send, the attempt wrote what one transaction writes,
    # as a run without the cancellation does up to its first transaction's send.
    control_key = keyed("carried-control", store)
    control = await attempt("two", await seeded(control_key), control_key)
    assert returned(control) == "ok,ok,ok", describe(control)
    assert writes_before_send(r) == writes_before_send(control), f"{describe(r)}\ncontrol: {describe(control)}"
    # And that is one commit: its send is inside it.
    assert send_is_inside_commit(r.sent(), store, 0), describe(r)


# --- A shutdown at the commit's ack


async def shut_at_ack(key: str) -> tuple[RestateSide, dict[str, bytes]]:
    """Run ``noted`` on a seeded key until its commit asks for its ack, then shut the attempt down."""
    state = await seeded(key)
    # Seeded under the same key, the pages are in the session's cache: the attempt asks for nothing before the ack
    # of its transaction's commit, the first thing it asks for.
    r = RestateSide(state, id_per_call=True, withhold_answer=1)
    r.start(key, None)
    running = asyncio.ensure_future(run(ledger, "noted", r))
    await r.until(lambda: len(r.asks) >= 1, "the commit asking for its ack")
    assert r.asks[0] == "ack", r.asks
    r.shut_down()
    await running
    return r, state


async def test_4_shut_down_while_the_commits_ack_is_outstanding_not_the_cancellation_and_the_next_attempt_finds_rows_and_send_agreeing(  # noqa: E501
    store: str, seen: Seen
):
    key = keyed("shut", store)
    r, state = await shut_at_ack(key)
    # The attempt's end, which is neither a cancellation nor terminal.
    assert len(seen.seen) == 1, seen.seen
    assert seen.seen[0] != "cancelled"
    assert seen.terminal == [False], seen.seen
    assert outcome(r.sent()) is None, describe(r)

    # A plain retry: its open makes what the outbox holds.
    again = await attempt("seed", state, key)
    assert returned(again) == "seeded", describe(again)
    stored = await stored_rows(state, key)
    sends = [*sends_of(r, "tx"), *sends_of(again, "tx")]
    # A stored row has its send, made by one attempt or both, always with the one key, so Restate makes it once; no
    # row, no send at all.
    assert len({s.idempotency_key for s in sends}) == (1 if "tx" in stored else 0), (stored, sends)


async def test_4b_the_commits_wait_fails_at_a_shutdown_as_a_calls_await_does(store: str, seen: Seen):
    # The commit's wait is the store's: at the attempt's end it fails as an await of the driver's does, so a handler
    # tells a shutdown from an error of its own.
    await shut_at_ack(keyed("shut2", store))
    commit = list(seen.seen)

    seen.seen.clear()
    key = keyed("shut2-call", store)
    r = RestateSide(await seeded(key), id_per_call=True, hold=["Slow/work"])
    r.start(key, None)
    running = asyncio.ensure_future(run(ledger, "awaits_a_call", r))
    await r.until(lambda: r.has_called("Slow/work"), "the call")
    r.shut_down()
    await running
    call = list(seen.seen)

    assert commit == call and len(commit) == 1, f"the commit's wait: {commit}; a call's await: {call}"


async def test_4c_a_shutdown_at_the_commits_ack_cancels_the_handlers_task(store: str, seen: Seen):
    # As for a call's await (``sessions_saga``, h): the handler's task is cancelled, where @restatedev/sessions
    # rejects the commit with ``AttemptShutDown``.
    await shut_at_ack(keyed("shut3", store))
    assert seen.seen == ["asyncio.exceptions.CancelledError"]


# --- A body that raises


async def test_5_a_body_that_raises_with_the_signal_arriving_during_it_no_row_no_send_and_one_cancellation_after_it(
    store: str, seen: Seen
):
    key = keyed("throws", store)
    state = await seeded(key)
    seen.hold()
    r = RestateSide(state, id_per_call=True)
    r.start(key, None)
    running = asyncio.ensure_future(run(ledger, "throws", r))
    await r.until(seen.reached.is_set, "the body working outside Restate")
    # Nothing of Restate's is outstanding, so the cancellation is carried; this returns once the attempt has it.
    await r.cancel()
    seen.opened.set()
    await running
    # The body's own error, then the cancellation at the next call of the database.
    assert returned(r) == "ValueError,cancelled,ok", describe(r)
    assert await stored_rows(state, key) == []
    assert r.sends == []


async def test_5b_a_body_that_raises_with_the_signal_carried_into_it_refused_before_it_begins_and_nothing_else_fails(
    store: str,
):
    key = keyed("throws2", store)
    state = await seeded(key)
    r = await attempt("throws", state, key, cancel_at_start=True)
    assert returned(r) == "cancelled,ok,ok", describe(r)
    assert await stored_rows(state, key) == ["after"]
    assert r.sends == []
