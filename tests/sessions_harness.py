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
"""Session objects against a real Restate server: no journal replay, concurrent awaits, and the outbox."""

# pylint: disable=missing-function-docstring

import asyncio
import typing
import uuid
from datetime import timedelta

import httpx2

import pytest

import restate
from restate import HarnessEnvironment, sessions
from restate.sessions import internal
from restate.sessions import _object, _session


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

# The writes that went through the Python store, rather than straight to the VM.
python_writes = 0


@pytest.fixture(autouse=True, params=["vm", "python"])
def store(request, monkeypatch):
    """
    Each test runs on both stores: the invocation's VM, which the extension
    writes and reads directly, and the Python store over the context's state
    commands, which an endpoint with a journal codec uses. The app serves in
    this process, so the patch reaches its handlers.
    """
    global python_writes  # pylint: disable=global-statement
    python_writes = 0
    if request.param == "python":
        monkeypatch.setattr(_object._ContextHost, "vm", property(lambda self: None))
    set_state = _session._ObjectStore.set

    def counted(self, key: str, value: bytes) -> None:
        global python_writes  # pylint: disable=global-statement
        python_writes += 1
        set_state(self, key, value)

    monkeypatch.setattr(_session._ObjectStore, "set", counted)
    return request.param


# --- A regular object, which session objects call and send to -----------------------------------

inbox = restate.VirtualObject("inbox")


@inbox.handler()
async def deliver(ctx: restate.ObjectContext, message: str) -> None:
    messages = await ctx.get("messages", type_hint=list[str]) or []
    ctx.set("messages", [*messages, message])


@inbox.handler(kind="shared")
async def messages(ctx: restate.ObjectSharedContext) -> list[str]:
    return await ctx.get("messages", type_hint=list[str]) or []


pricing = restate.Service("pricing")


@pricing.handler()
async def price(ctx: restate.Context, sku: str) -> int:
    await asyncio.sleep(0.05)
    return len(sku)


# --- Session objects ----------------------------------------------------------------------------

notes = sessions.SessionObject("notes", migrations=["CREATE TABLE notes (text TEXT NOT NULL)"])


@notes.handler()
async def add(ctx: sessions.SessionContext, text: str) -> int:
    await ctx.db.execute("INSERT INTO notes (text) VALUES (?)", [text])
    row = await ctx.db.get("SELECT count(*) AS n FROM notes")
    assert row is not None
    return typing.cast(int, row["n"])


@notes.view()
async def texts(ctx: sessions.ViewContext) -> list[str]:
    return [typing.cast(str, row["text"]) for row in await ctx.db.query("SELECT text FROM notes ORDER BY rowid")]


@notes.handler()
async def flaky(ctx: sessions.SessionContext, text: str) -> int:
    """Commits, then fails its first attempt: the retry runs from the start, and finds the commit."""
    seen = await ctx.db.get("SELECT count(*) AS n FROM notes WHERE text = ?", [text])
    if seen is not None and seen["n"] == 0:
        await ctx.db.execute("INSERT INTO notes (text) VALUES (?)", [text])
        raise ValueError("crash after the commit")
    return typing.cast(int, (await ctx.db.get("SELECT count(*) AS n FROM notes"))["n"])  # type: ignore[index]


@notes.handler()
async def priced(ctx: sessions.SessionContext, sku: str) -> list[int]:
    """A call and SQL awaited together, and two calls at once: one await point, the selector's."""
    first, second, row = await asyncio.gather(
        ctx.call(price, sku),
        ctx.call(price, sku * 2),
        ctx.db.get("SELECT count(*) AS n FROM notes"),
    )
    assert row is not None
    return [first, second, typing.cast(int, row["n"])]


@notes.handler()
async def forward(ctx: sessions.SessionContext, text: str) -> None:
    """A note, and its delivery to the inbox of the same key, sent once the transaction commits."""
    async with ctx.db.transaction() as tx:
        await tx.execute("INSERT INTO notes (text) VALUES (?)", [text])
        tx.send(deliver, text, key=ctx.key)


sleeper = restate.Service("sleeper")


@sleeper.handler()
async def nap(ctx: restate.Context) -> str:
    await ctx.sleep(timedelta(minutes=5))
    return "rested"


@notes.handler(explicit_cancellation=True)
async def stoppable(ctx: sessions.SessionContext, text: str) -> list[str]:
    """Waits for a nap; cancelled, it cancels the nap, records that it stopped, and answers the calls it cancelled."""
    napping = asyncio.ensure_future(ctx.call(nap))
    stopping = asyncio.ensure_future(internal.cancelled(ctx))
    await asyncio.wait({napping, stopping}, return_when=asyncio.FIRST_COMPLETED)
    assert stopping.done()
    cancelled = await internal.cancel_calls(ctx)
    await ctx.db.execute("INSERT INTO notes (text) VALUES (?)", [f"stopped: {text}"])
    return cancelled


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        sessions.app(inbox, pricing, sleeper, notes), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


async def test_a_handler_commits_and_a_view_reads_it(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    assert await harness.client.object_call(add, key=key, arg="milk") == 1
    assert await harness.client.object_call(add, key=key, arg="eggs") == 2
    assert await harness.client.object_call(texts, key=key, arg=None) == ["milk", "eggs"]


async def test_a_view_of_a_key_never_written_reads_empty_tables(harness: HarnessEnvironment):
    assert await harness.client.object_call(texts, key=str(uuid.uuid4()), arg=None) == []


async def test_a_retry_runs_the_handler_from_the_start_against_what_was_committed(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    assert await harness.client.object_call(flaky, key=key, arg="once") == 1
    assert await harness.client.object_call(texts, key=key, arg=None) == ["once"]


async def test_calls_and_sql_are_awaited_together(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    await harness.client.object_call(add, key=key, arg="x")
    assert await harness.client.object_call(priced, key=key, arg="ab") == [2, 4, 1]


async def test_the_sends_of_a_transaction_are_made_once_it_commits(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    await harness.client.object_call(forward, key=key, arg="hello")
    assert await harness.client.object_call(texts, key=key, arg=None) == ["hello"]
    delivered: list[str] = []
    for _ in range(50):
        delivered = await harness.client.object_call(messages, key=key, arg=None)
        if delivered:
            break
        await asyncio.sleep(0.1)
    assert delivered == ["hello"]


async def test_the_writes_go_through_the_store_asked_for(harness: HarnessEnvironment, store: str):
    await harness.client.object_call(add, key=str(uuid.uuid4()), arg="x")
    assert (python_writes > 0) == (store == "python")


async def cancel_when_running(harness: HarnessEnvironment, invocation_id: str) -> None:
    """Cancel the invocation, once Restate runs it."""
    async with httpx2.AsyncClient() as admin:
        for _ in range(100):
            status = await admin.post(
                f"{harness.admin_api_url}/query",
                json={"query": f"SELECT status FROM sys_invocation WHERE id = '{invocation_id}'"},
                headers={"Accept": "application/json"},
            )
            rows = status.json().get("rows", [])
            if rows and rows[0]["status"] in ("running", "suspended"):
                break
            await asyncio.sleep(0.05)
        response = await admin.patch(f"{harness.admin_api_url}/invocations/{invocation_id}/cancel")
        response.raise_for_status()


async def attach(harness: HarnessEnvironment, invocation_id: str) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=30) as ingress:
        return await ingress.get(f"{harness.ingress_url}/restate/invocation/{invocation_id}/attach")


async def test_a_handler_with_explicit_cancellation_stops_its_own_work(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    sent = await harness.client.object_send(stoppable, key=key, arg="nap")
    await cancel_when_running(harness, sent.invocation_id)
    answer = await attach(harness, sent.invocation_id)
    assert answer.status_code == 200, answer.text
    [nap_id] = answer.json()
    assert (await attach(harness, nap_id)).status_code == 409
    assert await harness.client.object_call(texts, key=key, arg=None) == ["stopped: nap"]


@notes.view
async def stop_it(ctx: sessions.ViewContext, invocation_id: str) -> None:
    """A Stop button: a view cancels a running handler of its own object."""
    ctx.cancel(invocation_id)


async def test_a_view_cancels_a_handler_of_its_own_object(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    sent = await harness.client.object_send(stoppable, key=key, arg="nap")
    async with httpx2.AsyncClient() as admin:
        for _ in range(100):
            status = await admin.post(
                f"{harness.admin_api_url}/query",
                json={"query": f"SELECT status FROM sys_invocation WHERE id = '{sent.invocation_id}'"},
                headers={"Accept": "application/json"},
            )
            rows = status.json().get("rows", [])
            if rows and rows[0]["status"] in ("running", "suspended"):
                break
            await asyncio.sleep(0.05)
    await harness.client.object_call(stop_it, key=key, arg=sent.invocation_id)
    answer = await attach(harness, sent.invocation_id)
    assert answer.status_code == 200, answer.text
    assert await harness.client.object_call(texts, key=key, arg=None) == ["stopped: nap"]
