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
"""Session objects on the explicit recovery protocol, over several attempts of one invocation, with the shared
core's fake runtime as Restate (``sessions_wire``).

Each test runs real handlers through the object's runner, and lets the fake decide what Restate decides: a restart or
a suspension after a shutdown, a wake-up when what the invocation waits for arrives. The fake checks every message
against the protocol, and ``run`` fails a test on any violation. These are the scenarios of the protocol's
specification (its section 14) as a Python handler meets them.
"""

# pylint: disable=missing-function-docstring

import asyncio
import json
import typing
from collections.abc import Iterator

import pytest

from restate import TerminalError, sessions
from restate._internal import PyFailure  # pylint: disable=no-name-in-module
from restate.sessions import _driver, internal
from restate.sessions._object import HEAD_KEY
from sessions_wire import TYPE, RestateSide, decode, outcome, run  # pylint: disable=import-error


@pytest.fixture
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]


@pytest.fixture(autouse=True, params=["vm", "python"])
def store(request, monkeypatch):
    """Each test runs on both stores: the VM's, which the extension reads and writes directly, and the Python store
    over the context's state actions, which an endpoint with a journal codec uses."""
    if request.param == "python":
        monkeypatch.setattr(_driver.VmHost, "vm", property(lambda self: None))
    return request.param


class Gate:
    """Where a handler waits outside Restate for the test: ``reached`` once it is there, on until ``opened``."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.opened = asyncio.Event()
        self.attempts: list[int | None] = []
        """``ctx.request.attempt`` of each attempt that reached the gate."""

    async def wait(self) -> None:
        self.reached.set()
        await self.opened.wait()


_gates: list[Gate] = []


@pytest.fixture
def gate() -> Iterator[Gate]:
    made = Gate()
    _gates.append(made)
    yield made
    _gates.remove(made)


def keyed(name: str, store: str) -> str:
    """The test's key on this store: the page cache is the process's, and every test runs on both stores."""
    return f"{name}-{store}"


def describe(side: RestateSide) -> str:
    return " ".join(f"{m.type:#06x}{'!' if m.is_action and m.wants_result else ''}" for m in side.sent())


shop = sessions.SessionObject("Shop", migrations=["CREATE TABLE orders (id TEXT PRIMARY KEY, note TEXT)"])


@shop.handler()
async def order(ctx: sessions.SessionContext, id_: str) -> int:
    """Commits a row and a send to ``Shipping/ship`` in one transaction."""
    async with ctx.db.transaction() as tx:
        await tx.execute("INSERT INTO orders (id) VALUES (?)", [id_])
        tx.generic_send("Shipping", "ship", id_, key=id_)
    row = await ctx.db.get("SELECT count(*) AS n FROM orders")
    assert row is not None
    return int(row["n"] or 0)


@shop.handler()
async def order_with_rider(ctx: sessions.SessionContext, id_: str) -> int:
    """Registers a rider (``ride_commit``), then commits a row and a send: the rider's write rides that commit."""

    async def note(tx: sessions.Transaction) -> None:
        await tx.execute("INSERT INTO orders (id, note) VALUES (?, 'rider')", [f"{id_}-rider"])

    assert internal.ride_commit(ctx.db, "note", note)
    async with ctx.db.transaction() as tx:
        await tx.execute("INSERT INTO orders (id) VALUES (?)", [id_])
        tx.generic_send("Shipping", "ship", id_, key=id_)
    row = await ctx.db.get("SELECT count(*) AS n FROM orders")
    assert row is not None
    return int(row["n"] or 0)


@shop.handler()
async def charge(ctx: sessions.SessionContext, id_: str) -> str:
    """Calls ``Payments/charge`` with an idempotency key, so that a later attempt reaches the same invocation."""
    result = await ctx.generic_call("Payments", "charge", id_, key=id_, idempotency_key=f"{ctx.request.id}/charge")
    return f"{typing.cast(str, result)} on attempt {ctx.request.attempt}"


@shop.handler()
async def commit_then_wait(ctx: sessions.SessionContext, id_: str) -> str:
    """Commits its row once (recovering from the state on a later attempt), then works outside Restate at the gate."""
    gate = _gates[-1]
    if await ctx.db.get("SELECT id FROM orders WHERE id = ?", [id_]) is None:
        await ctx.db.execute("INSERT INTO orders (id) VALUES (?)", [id_])
    gate.attempts.append(ctx.request.attempt)
    await gate.wait()
    rows = await ctx.db.get("SELECT count(*) AS n FROM orders")
    assert rows is not None
    return f"{rows['n']!s} row(s), attempt {ctx.request.attempt}"


@shop.handler()
async def approve(ctx: sessions.SessionContext, id_: str) -> str:
    """Waits for the signal ``approval``, then records it."""
    payload = await internal.read_signal(ctx, "approval")
    decision = json.loads(payload) if payload is not None else None
    await ctx.db.execute("INSERT INTO orders (id, note) VALUES (?, ?)", [id_, str(decision)])
    return f"{decision} on attempt {ctx.request.attempt}"


@shop.handler()
async def notify(ctx: sessions.SessionContext, target: str) -> str:
    """Sends the invocation ``target`` the signal ``done``, and waits until Restate applied it."""
    internal.send_signal(ctx, target, "done", b'"shipped"')
    await internal.flush(ctx)
    return "notified"


@shop.handler()
async def progress(ctx: sessions.SessionContext, id_: str) -> str:
    """Keeps its progress in the invocation's own state: what an earlier attempt did, it skips."""
    state = internal.invocation_state(ctx)
    step = await state.get("step")
    if step is None:
        state.set("step", b"reserved")
        await internal.flush(ctx)
        await _gates[-1].wait()
    return f"{(await state.get('step') or b'').decode()} on attempt {ctx.request.attempt}"


@shop.handler()
async def awaits_a_call(ctx: sessions.SessionContext, id_: str) -> str:
    """A call Restate holds; what its await ended with."""
    try:
        await ctx.generic_call("Slow", "work", id_, key=id_)
        return "answered"
    except TerminalError as error:
        return f"{error.status_code} {error.message}"


@shop.handler()
async def annotated(ctx: sessions.SessionContext, id_: str) -> str:
    """Times its work as a span, inside another."""
    outer = internal.annotate(ctx, "turn")
    inner = internal.annotate(ctx, "model", parent=outer)
    inner.end({"tokens": 3})
    outer.fail("no tool")
    return "annotated"


@shop.view()
async def orders_of(ctx: sessions.ViewContext) -> list[str]:
    return [str(row["id"]) for row in await ctx.db.query("SELECT id FROM orders ORDER BY id")]


async def stored_orders(state: dict[str, bytes], key: str) -> list[str]:
    side = RestateSide(state)
    side.start(key, None)
    await run(shop, "orders_of", side)
    ended = outcome(side.sent())
    assert ended is not None and ended[0] == "value", ended
    return list(ended[1])


# --- A commit with a send


async def test_a_commit_and_its_send_are_one_atomic_group(store: str):
    key = keyed("commit", store)
    side = RestateSide()
    side.start(key, "o1")
    await run(shop, "order", side)
    assert outcome(side.sent()) == ("value", 1), describe(side)
    assert side.rt is not None and side.rt.status()[0] == "completed"
    # The transaction's commit: one group, its writes (and the database's head) with the send after them.
    groups = [m for m in side.sent() if m.type == TYPE.atomic_group]
    with_send = [g for g in groups if any(a.type == TYPE.call for a in g.members())]
    assert len(with_send) == 1, describe(side)
    members = [a.type for a in with_send[0].members()]
    assert TYPE.set_state in members[: members.index(TYPE.call)], members
    assert with_send[0].wants_result and with_send[0].text_of(12) == "commit"
    # Every write of the database is recorded as nothing (RECORD_NONE = 1), and wants no result.
    writes = [a for g in groups for a in g.members() if a.type in (TYPE.set_state, TYPE.clear_state)]
    assert writes and all(a.int_of(14) == 1 and not a.wants_result for a in writes)
    # No outbox, no key of the SDK's own: the send as the handler made it, once.
    assert [(s.target, s.key, s.idempotency_key, s.input) for s in side.sends] == [
        ("Shipping/ship", "o1", None, '"o1"')
    ]
    assert HEAD_KEY in side.state
    assert await stored_orders(side.state, key) == ["o1"]


async def test_a_rider_rides_the_commit_and_its_send_in_one_atomic_group(store: str):
    key = keyed("rider", store)
    side = RestateSide()
    side.start(key, "o1")
    await run(shop, "order_with_rider", side)
    assert outcome(side.sent()) == ("value", 2), describe(side)
    # One commit carries the transaction's row, the rider's, and the send: no commit of the rider's own, so as
    # many groups as the same transaction without a rider.
    groups = [m for m in side.sent() if m.type == TYPE.atomic_group]
    with_send = [g for g in groups if any(a.type == TYPE.call for a in g.members())]
    assert len(with_send) == 1, describe(side)
    plain = RestateSide()
    plain.start(keyed("rider-plain", store), "o1")
    await run(shop, "order", plain)
    assert len(groups) == len([m for m in plain.sent() if m.type == TYPE.atomic_group]), describe(side)
    assert [(s.target, s.key) for s in side.sends] == [("Shipping/ship", "o1")]
    assert sorted(await stored_orders(side.state, key)) == ["o1", "o1-rider"]


# --- A call that outlives its attempt


async def test_a_call_that_outlives_its_attempt_is_answered_to_the_next_one(store: str):
    side = RestateSide(hold=["Payments/charge"])
    side.start(keyed("outlives", store), "o1")
    first = asyncio.ensure_future(run(shop, "charge", side))
    await side.until(lambda: side.has_called("Payments/charge"), "the charge call")
    await side.quiet()
    # Silent for the inactivity timeout, with the call's result outstanding: suspended (rule 6).
    side.shut_down("inactivity")
    await first
    assert outcome(side.sent()) is None, describe(side)
    assert side.sent()[-1].type == TYPE.shutdown, describe(side)
    rt = side.rt
    assert rt is not None
    assert rt.outcomes() == [("suspended",)]
    assert rt.status() == "suspended"
    # The callee returns: the invocation wakes up.
    callee = side.calls[0].invocation_id
    rt.complete_call(callee, b'"charged"')
    assert rt.status() == "ready"
    # The next attempt calls again, with the same idempotency key: the same invocation, which returned already.
    await run(shop, "charge", side)
    assert outcome(side.sent()) == ("value", "charged on attempt 2"), describe(side)
    assert len(rt.calls()) == 1
    assert rt.outcomes() == [("suspended",), ("completed",)]


# --- A restart, from the state


async def test_a_restarted_attempt_runs_the_handler_again_from_what_is_stored(store: str, gate: Gate):
    key = keyed("restart", store)
    side = RestateSide()
    side.start(key, "o1")
    first = asyncio.ensure_future(run(shop, "commit_then_wait", side))
    await gate.reached.wait()
    await side.quiet()
    # Restate's own reason (a drain, say): restarted at once, no retry counted (rule 1).
    side.shut_down("own")
    await first
    rt = side.rt
    assert rt is not None
    assert rt.outcomes() == [("restarted", 1)], describe(side)
    # The second attempt finds the row the first committed, and does not insert it again.
    gate.opened.set()
    await run(shop, "commit_then_wait", side)
    assert outcome(side.sent()) == ("value", "1 row(s), attempt 2"), describe(side)
    assert gate.attempts == [1, 2]


async def test_a_sigterm_ends_the_attempt_with_an_unasked_shutdown_and_restate_restarts_it(store: str, gate: Gate):
    side = RestateSide()
    side.start(keyed("sigterm", store), "o1")
    first = asyncio.ensure_future(run(shop, "commit_then_wait", side))
    await gate.reached.wait()
    await side.quiet()
    assert side.channel is not None
    side.channel.notify_shutdown()
    await first
    assert side.sent()[-1].type == TYPE.shutdown, describe(side)
    rt = side.rt
    assert rt is not None
    assert rt.status() == "ready", rt.status()
    assert rt.outcomes()[-1][0] == "restarted", rt.outcomes()
    gate.opened.set()
    await run(shop, "commit_then_wait", side)
    assert outcome(side.sent()) == ("value", "1 row(s), attempt 2"), describe(side)


# --- A suspension, and the wake-up of a signal


async def test_a_signal_read_suspends_the_invocation_and_its_arrival_wakes_it(store: str):
    key = keyed("signal", store)
    side = RestateSide()
    side.start(key, "o1")
    first = asyncio.ensure_future(run(shop, "approve", side))
    await side.until(lambda: any(m.type == TYPE.read_signal for m in side.sent()), "the signal read")
    await side.quiet()
    side.shut_down("inactivity")
    await first
    rt = side.rt
    assert rt is not None
    assert rt.status() == "suspended", rt.outcomes()
    read = next(m for m in side.sent() if m.type == TYPE.read_signal)
    assert (read.text_of(1), read.int_of(2)) == ("approval", 0)
    # The signal arrives: the invocation wakes, and its next attempt reads it.
    rt.send_signal("approval", b'"yes"')
    assert rt.status() == "ready"
    await run(shop, "approve", side)
    assert outcome(side.sent()) == ("value", "yes on attempt 2"), describe(side)
    assert await stored_orders(side.state, key) == ["o1"]


async def test_a_signal_sent_to_another_invocation(store: str):
    side = RestateSide()
    side.start(keyed("notify", store), "inv_other")
    await run(shop, "notify", side)
    assert outcome(side.sent()) == ("value", "notified"), describe(side)
    rt = side.rt
    assert rt is not None
    [(target, name, value)] = rt.signals_sent()
    assert (target, name, value) == ("inv_other", "done", b'"shipped"')


async def test_a_signal_that_arrived_before_the_read_answers_it_at_once(store: str):
    side = RestateSide()
    side.start(keyed("early", store), "o1")
    side.runtime(shop, "approve").send_signal("approval", PyFailure(403, "denied"))
    await run(shop, "approve", side)
    # A signal that carries a failure raises it in the handler: the invocation ends with it.
    assert outcome(side.sent()) == ("failure", 403, "denied"), describe(side)


# --- The invocation's own state


async def test_invocation_state_carries_progress_across_attempts(store: str, gate: Gate):
    side = RestateSide()
    side.start(keyed("progress", store), "o1")
    first = asyncio.ensure_future(run(shop, "progress", side))
    await gate.reached.wait()
    await side.quiet()
    side.shut_down("own")
    await first
    rt = side.rt
    assert rt is not None
    assert dict(rt.invocation_state()) == {"step": b"reserved"}
    # Invocation state is the invocation's: it is not the object's.
    assert "step" not in side.state
    await run(shop, "progress", side)
    assert outcome(side.sent()) == ("value", "reserved on attempt 2"), describe(side)


# --- Cancellation


async def test_a_cancellation_cancels_the_call_under_way_and_fails_its_await(store: str):
    side = RestateSide(hold=["Slow/work"])
    side.start(keyed("cancel", store), "o1")
    running = asyncio.ensure_future(run(shop, "awaits_a_call", side))
    await side.until(lambda: side.has_called("Slow/work"), "the call")
    await side.cancel()
    await running
    assert outcome(side.sent()) == ("value", "409 cancelled"), describe(side)
    # The call Restate had not answered is cancelled.
    assert side.cancels == [side.calls[0].invocation_id], describe(side)


async def test_a_suspended_invocation_wakes_on_its_cancellation_and_the_next_attempt_knows_it_from_the_start(
    store: str,
):
    side = RestateSide(hold=["Slow/work"])
    side.start(keyed("cancel-suspended", store), "o1")
    first = asyncio.ensure_future(run(shop, "awaits_a_call", side))
    await side.until(lambda: side.has_called("Slow/work"), "the call")
    await side.quiet()
    side.shut_down("inactivity")
    await first
    rt = side.rt
    assert rt is not None and rt.status() == "suspended"
    rt.cancel()
    assert rt.status() == "ready"
    # StartMessage.cancelled: the first await fails with the cancellation.
    await run(shop, "awaits_a_call", side)
    assert outcome(side.sent()) == ("value", "409 cancelled"), describe(side)


# --- Spans


async def test_annotations_are_spans(store: str):
    side = RestateSide()
    side.start(keyed("spans", store), "o1")
    await run(shop, "annotated", side)
    assert outcome(side.sent()) == ("value", "annotated"), describe(side)
    starts = [m for m in side.sent() if m.type == TYPE.span_start]
    ends = [m for m in side.sent() if m.type == TYPE.span_end]
    assert [m.text_of(12) for m in starts] == ["turn", "model"]
    outer, inner = (m.int_of(1) for m in starts)
    # The inner span is inside the outer one.
    assert starts[1].int_of(13) == outer
    # Ended: the inner with its summary, the outer with its failure.
    assert [m.int_of(1) for m in ends] == [inner, outer]
    summary = decode(ends[0].bytes_of(3))[1][0]
    assert isinstance(summary, bytes) and json.loads(summary) == {"tokens": 3}
    assert 4 in ends[1].fields
