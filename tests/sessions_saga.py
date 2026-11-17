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
"""A saga written as a session object, run on the wire (``sessions_wire``).

Its progress lives in ``ctx.db``, every attempt resumes from what is stored,
and a cancellation or a terminal failure compensates the steps that may have
happened, in reverse, with idempotency keys of their own.
"""

# pylint: disable=missing-function-docstring

import asyncio
from collections.abc import Iterator
from typing import Any, Optional

import pytest

from restate import TerminalError, sessions
from restate.sessions import _driver
from sessions_wire import TYPE, Made, RestateSide, commits, outcome, run  # pylint: disable=import-error


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


UNDO = {"reserve": ("Inventory", "release"), "charge": ("Payments", "refund")}
"""Each step, and the call that undoes it."""

HAPPENED = {"started": ["reserve"], "reserved": ["charge", "reserve"], "charged": ["charge", "reserve"]}
"""The steps that may have happened by an order's status, last first."""


def is_cancellation(error: BaseException) -> bool:
    return isinstance(error, TerminalError) and error.status_code == 409 and error.message == "cancelled"


def name_of(error: BaseException) -> str:
    """What ``place`` caught, as a test compares it: ``cancelled``, or the class's qualified name."""
    return "cancelled" if is_cancellation(error) else f"{type(error).__module__}.{type(error).__qualname__}"


class Outside:
    """Work the handler does outside Restate between its two steps, and what it caught; a test holds it there."""

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.opened = asyncio.Event()
        self.opened.set()
        self.caught: list[str] = []
        self.terminal: list[bool] = []
        """Whether each error ``caught`` has is a ``TerminalError``, which the saga compensates on."""

    def hold(self) -> None:
        self.opened.clear()

    async def work(self) -> None:
        self.reached.set()
        await self.opened.wait()


_outside: list[Outside] = []


@pytest.fixture(autouse=True)
def outside() -> Iterator[Outside]:
    made = Outside()
    _outside.append(made)
    yield made
    _outside.remove(made)


orders = sessions.SessionObject(
    "Orders", migrations=["CREATE TABLE orders (id TEXT PRIMARY KEY, status TEXT NOT NULL, undo TEXT)"]
)


@orders.handler()
async def place(ctx: sessions.SessionContext, id_: str) -> str:
    here = _outside[-1]

    def call(service: str, handler: str, step: str) -> Any:
        # The same on every attempt: a repeated call reaches the invocation the first attempt started.
        return ctx.generic_call(service, handler, id_, key=id_, idempotency_key=f"{ctx.request.id}/{step}")

    async def read() -> Optional[dict[str, Any]]:
        return await ctx.db.get("SELECT status, undo FROM orders WHERE id = ?", [id_])

    async def record(status: str) -> None:
        await ctx.db.execute("UPDATE orders SET status = ? WHERE id = ?", [status, id_])

    # A step whose call failed terminally did not happen, and has nothing to undo.
    did_not_happen: Optional[str] = None

    async def step(name: str, service: str, handler: str) -> None:
        nonlocal did_not_happen
        try:
            await call(service, handler, name)
        except TerminalError as error:
            if not is_cancellation(error):
                did_not_happen = name
            raise

    async def compensate() -> None:
        # Undo, from what is stored: a retry that finds the saga compensating goes on with the same list, and calls
        # again with the same keys.
        order = await read()
        if order is None or order["status"] == "compensated":
            return
        undo = order["undo"] or ""
        if order["status"] != "compensating":
            undo = ",".join(s for s in HAPPENED[order["status"]] if s != did_not_happen)
            await ctx.db.execute("UPDATE orders SET status = 'compensating', undo = ? WHERE id = ?", [undo, id_])
        for s in filter(None, undo.split(",")):
            service, handler = UNDO[s]
            await call(service, handler, f"undo-{s}")
        await record("compensated")

    try:
        order = await read()
        if order is None:
            async with ctx.db.transaction() as tx:
                await tx.execute("INSERT INTO orders (id, status) VALUES (?, 'started')", [id_])
            order = {"status": "started", "undo": None}
        if order["status"] in ("compensating", "compensated"):
            raise TerminalError(f"order {id_} was cancelled")
        if order["status"] == "started":
            await step("reserve", "Inventory", "reserve")
            await here.work()
            await record("reserved")
            order["status"] = "reserved"
        if order["status"] == "reserved":
            await step("charge", "Payments", "charge")
            await record("charged")
        return "charged"
    except BaseException as error:
        here.caught.append(name_of(error))
        here.terminal.append(isinstance(error, TerminalError))
        # A cancellation is a TerminalError; the attempt's end (a shutdown) is not, and the next attempt resumes:
        # nothing to compensate.
        if not isinstance(error, TerminalError):
            raise
        await compensate()
        raise


@orders.view()
async def status(ctx: sessions.ViewContext, id_: str) -> Optional[dict[str, Any]]:
    return await ctx.db.get("SELECT status, undo FROM orders WHERE id = ?", [id_])


async def stored(state: dict[str, bytes], key: str, id_: str) -> Optional[dict[str, Any]]:
    """The stored row of order ``id_``, read by a view on ``state``."""
    side = RestateSide(state)
    side.start(key, id_)
    await run(orders, "status", side)
    ended = outcome(side.sent())
    assert ended is not None and ended[0] == "value", ended
    return ended[1]


def target(c: Made) -> str:
    """A call as a test compares it: its target and idempotency key."""
    return f"{c.target} {c.idempotency_key}"


def describe(side: RestateSide) -> str:
    """What the attempt wrote, as message types in order: for a failure's message."""
    return " ".join(f"{m.type:#06x}" for m in side.sent())


class Placing:
    """An attempt of ``place`` for an order, running while the test plays Restate's side of it."""

    def __init__(self, state: dict[str, bytes], key: str, id_: str, **options: Any) -> None:
        self.side = RestateSide(state, id_per_call=True, **options)
        self.side.start(key, id_)
        self.done = asyncio.ensure_future(run(orders, "place", self.side))


CANCELLED = ("failure", 409, "cancelled")
"""The invocation's end with the cancellation, as Restate is told it."""


def keyed(name: str, store: str) -> str:
    """The test's key on this store: the page cache is the process's, and every test runs on both stores."""
    return f"{name}-{store}"


async def test_a_without_a_cancellation_reserves_and_charges_once_each_and_ends_charged(store: str):
    state: dict[str, bytes] = {}
    key = keyed("sa", store)
    p = Placing(state, key, "o1")
    await p.done
    r = p.side
    assert outcome(r.sent()) == ("value", "charged"), describe(r)
    assert [target(c) for c in r.calls] == ["Inventory/reserve inv_test/reserve", "Payments/charge inv_test/charge"]
    assert r.cancels == []
    assert await stored(state, key, "o1") == {"status": "charged", "undo": None}


async def test_b_cancelled_while_reserve_is_pending_it_is_cancelled_released_and_the_invocation_ends_cancelled(
    store: str,
):
    state: dict[str, bytes] = {}
    key = keyed("sb", store)
    p = Placing(state, key, "o1", hold=["Inventory/reserve"])
    r = p.side
    await r.until(lambda: r.has_called("Inventory/reserve"), "the reserve call")
    await r.cancel()
    await p.done
    assert outcome(r.sent()) == CANCELLED, describe(r)
    assert [target(c) for c in r.calls] == [
        "Inventory/reserve inv_test/reserve",
        "Inventory/release inv_test/undo-reserve",
    ]
    # The cancel command names the reserve call's invocation.
    assert r.cancels == [r.calls[0].invocation_id], describe(r)
    assert await stored(state, key, "o1") == {"status": "compensated", "undo": "reserve"}


async def test_c_cancelled_while_charge_is_pending_refunds_then_releases(store: str):
    state: dict[str, bytes] = {}
    key = keyed("sc", store)
    p = Placing(state, key, "o1", hold=["Payments/charge"])
    r = p.side
    await r.until(lambda: r.has_called("Payments/charge"), "the charge call")
    await r.cancel()
    await p.done
    assert outcome(r.sent()) == CANCELLED, describe(r)
    assert [target(c) for c in r.calls] == [
        "Inventory/reserve inv_test/reserve",
        "Payments/charge inv_test/charge",
        "Payments/refund inv_test/undo-charge",
        "Inventory/release inv_test/undo-reserve",
    ]
    assert r.cancels == [r.calls[1].invocation_id], describe(r)
    assert await stored(state, key, "o1") == {"status": "compensated", "undo": "charge,reserve"}


async def test_d_known_limitation_a_signal_on_the_commit_of_reserved_is_carried_to_the_charge_call_which_is_issued_fails_and_is_not_cancelled(  # noqa: E501
    store: str,
):
    # The commit that records `reserved` is under way when the signal comes: it lands, and the cancellation is
    # carried to the next await, the `charge` call. That call is still issued (it is on the wire) and its result
    # fails with the cancellation at once, but no cancel command is sent for it, so the charge may run. The saga then
    # compensates both steps, so a charge that did run is refunded.
    # On a fresh key the attempt's commits are the schema's, `started`'s and `reserved`'s: the signal goes in with
    # the third.
    state: dict[str, bytes] = {}
    key = keyed("sd", store)
    p = Placing(state, key, "o1", cancel_on_commit=3)
    await p.done
    r = p.side
    sent = r.sent()
    assert outcome(sent) == CANCELLED, describe(r)
    # The premise: before the signal, three commits and the reserve call; the first command after it is the charge
    # call.
    assert r.signalled_at is not None, describe(r)
    assert commits(sent[: r.signalled_at]) == 3, describe(r)
    after = next((m for m in sent[r.signalled_at :] if 0x0400 <= m.type < 0x8000), None)
    assert after is not None and after.type == TYPE.call, describe(r)
    assert [target(c) for c in r.calls] == [
        "Inventory/reserve inv_test/reserve",
        "Payments/charge inv_test/charge",
        "Payments/refund inv_test/undo-charge",
        "Inventory/release inv_test/undo-reserve",
    ]
    # The stray charge: no cancel command for it, nor for anything.
    assert r.cancels == [], describe(r)
    assert await stored(state, key, "o1") == {"status": "compensated", "undo": "charge,reserve"}


async def test_e_cancelled_while_working_outside_restate_the_next_db_call_is_refused_and_it_compensates_from_started(
    store: str, outside: Outside
):
    state: dict[str, bytes] = {}
    key = keyed("se", store)
    outside.hold()
    p = Placing(state, key, "o1")
    r = p.side
    await r.until(outside.reached.is_set, "the work between the steps")
    # Nothing of Restate's is outstanding, so the cancellation is carried; this returns once the attempt has it.
    await r.cancel()
    before = len(r.sent())
    outside.opened.set()
    await p.done
    assert outcome(r.sent()) == CANCELLED, describe(r)
    assert outside.caught == ["cancelled"]
    # The refused `reserved` wrote nothing: the first command after the signal is the write of `compensating`'s
    # commit, whose row says `started` was the last step recorded.
    assert [target(c) for c in r.calls] == [
        "Inventory/reserve inv_test/reserve",
        "Inventory/release inv_test/undo-reserve",
    ]
    assert r.cancels == []
    assert before == r.signalled_at, describe(r)
    # After the signal, two commits: `compensating`'s and `compensated`'s.
    assert commits(r.sent()[before:]) == 2, describe(r)
    assert await stored(state, key, "o1") == {"status": "compensated", "undo": "reserve"}


async def test_f_a_crash_in_the_middle_of_compensating_the_retry_of_the_cancelled_invocation_finishes_it_with_the_same_keys(  # noqa: E501
    store: str, outside: Outside
):
    state: dict[str, bytes] = {}
    key = keyed("sf", store)
    first = Placing(state, key, "o1", hold=["Inventory/reserve", "Inventory/release"])
    r1 = first.side
    await r1.until(lambda: r1.has_called("Inventory/reserve"), "the reserve call")
    await r1.cancel()
    await r1.until(lambda: r1.has_called("Inventory/release"), "the release call")
    # `compensating` was committed before `release` was called.
    r1.shut_down()
    await first.done
    assert outcome(r1.sent()) is None, describe(r1)
    assert await stored(state, key, "o1") == {"status": "compensating", "undo": "reserve"}

    outside.caught.clear()
    second = Placing(state, key, "o1", cancel_at_start=True)
    await second.done
    r2 = second.side
    # Its first await, the read of the stored state, got the cancellation.
    assert outside.caught == ["cancelled"]
    assert outcome(r2.sent()) == CANCELLED, describe(r2)
    assert [target(c) for c in r2.calls] == ["Inventory/release inv_test/undo-reserve"]
    assert r2.calls[0].idempotency_key == r1.calls[1].idempotency_key
    assert await stored(state, key, "o1") == {"status": "compensated", "undo": "reserve"}


async def test_g_a_step_that_fails_terminally_only_the_step_before_it_is_undone_and_the_invocation_ends_with_that_failure(  # noqa: E501
    store: str,
):
    state: dict[str, bytes] = {}
    key = keyed("sg", store)
    p = Placing(state, key, "o1", fail={"Payments/charge": (402, "declined")})
    await p.done
    r = p.side
    assert outcome(r.sent()) == ("failure", 402, "declined"), describe(r)
    assert [target(c) for c in r.calls] == [
        "Inventory/reserve inv_test/reserve",
        "Payments/charge inv_test/charge",
        "Inventory/release inv_test/undo-reserve",
    ]
    assert r.cancels == []
    assert await stored(state, key, "o1") == {"status": "compensated", "undo": "reserve"}


async def shut_down_while_reserve_is_outstanding(state: dict[str, bytes], key: str) -> RestateSide:
    """Run ``place`` until its reserve call is outstanding, then shut the attempt down."""
    first = Placing(state, key, "o1", hold=["Inventory/reserve"])
    r1 = first.side
    await r1.until(lambda: r1.has_called("Inventory/reserve"), "the reserve call")
    r1.shut_down()
    await first.done
    return r1


async def test_h_shut_down_while_reserve_is_outstanding_no_compensation_and_the_next_attempt_resumes_with_the_same_key(  # noqa: E501
    store: str, outside: Outside
):
    state: dict[str, bytes] = {}
    key = keyed("sh", store)
    r1 = await shut_down_while_reserve_is_outstanding(state, key)
    # The await failed with the attempt's end, which is no TerminalError.
    assert len(outside.caught) == 1 and outside.caught != ["cancelled"], outside.caught
    assert outside.terminal == [False], outside.caught
    assert outcome(r1.sent()) is None, describe(r1)
    assert [target(c) for c in r1.calls] == ["Inventory/reserve inv_test/reserve"]
    assert await stored(state, key, "o1") == {"status": "started", "undo": None}

    second = Placing(state, key, "o1")
    await second.done
    r2 = second.side
    assert outcome(r2.sent()) == ("value", "charged"), describe(r2)
    assert [target(c) for c in r2.calls] == [
        "Inventory/reserve inv_test/reserve",
        "Payments/charge inv_test/charge",
    ]
    assert r2.calls[0].idempotency_key == r1.calls[0].idempotency_key
    assert await stored(state, key, "o1") == {"status": "charged", "undo": None}


async def test_h_a_shutdown_cancels_the_handlers_task(store: str, outside: Outside):
    # The end of an attempt cancels the handler's task (``AttemptDriver._end``), so what the handler's await raises
    # is asyncio's cancellation: not a ``TerminalError``, which is what keeps the saga from undoing anything. This is
    # where Python differs from @restatedev/sessions, whose await rejects with ``AttemptShutDown``.
    await shut_down_while_reserve_is_outstanding({}, keyed("sh2", store))
    assert outside.caught == ["asyncio.exceptions.CancelledError"]
