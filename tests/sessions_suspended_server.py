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
"""A session object's invocation that Restate shut down while it waited for a call, and is then cancelled."""

# pylint: disable=missing-function-docstring

import asyncio
import typing
import uuid
from datetime import timedelta
from typing import Any

import httpx2
import pytest

import restate
from restate import HarnessEnvironment, sessions
from restate.sessions import internal
from support import RESTATE_IMAGE  # pylint: disable=import-error


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

sleeper = restate.Service("sleeper")


@sleeper.handler()
async def nap(ctx: restate.Context) -> str:
    await ctx.sleep(timedelta(minutes=5))
    return "rested"


# Restate shuts down an attempt it heard nothing from for a second.
waiting = sessions.SessionObject(
    "waiting", migrations=["CREATE TABLE seen (what TEXT NOT NULL)"], inactivity_timeout=timedelta(seconds=1)
)
attempts: dict[str, int] = {}


@waiting.handler()
async def waits(ctx: sessions.SessionContext) -> str:
    """Waits for a call that takes minutes, without explicit cancellation."""
    attempts[ctx.key] = attempts.get(ctx.key, 0) + 1
    return await ctx.call(nap, idempotency_key=ctx.request.id)


@waiting.handler(explicit_cancellation=True)
async def waits_and_watches(ctx: sessions.SessionContext) -> str:
    """Waits for a call that takes minutes, or for its cancellation, which it handles itself."""
    attempts[ctx.key] = attempts.get(ctx.key, 0) + 1
    napping = asyncio.ensure_future(ctx.call(nap, idempotency_key=ctx.request.id))
    stopping = asyncio.ensure_future(internal.cancelled(ctx))
    await asyncio.wait({napping, stopping}, return_when=asyncio.FIRST_COMPLETED)
    if not stopping.done():
        return await napping
    await internal.cancel_calls(ctx)
    await ctx.db.execute("INSERT INTO seen (what) VALUES ('stopped')")
    return "stopped"


@waiting.handler(explicit_cancellation=True)
async def waits_whatever_comes(ctx: sessions.SessionContext) -> str:
    """Waits for a call that takes minutes, and goes on waiting for it once cancelled."""
    attempts[ctx.key] = attempts.get(ctx.key, 0) + 1
    return await ctx.call(nap, idempotency_key=ctx.request.id)


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(restate.app([sleeper, waiting]), restate_image=RESTATE_IMAGE) as harness:
        yield harness


async def status(admin: httpx2.AsyncClient, harness: HarnessEnvironment, invocation_id: str) -> str | None:
    answered = await admin.post(
        f"{harness.admin_api_url}/query",
        json={"query": f"SELECT status FROM sys_invocation WHERE id = '{invocation_id}'"},
        headers={"Accept": "application/json"},
    )
    rows = typing.cast("list[dict[str, Any]]", answered.json().get("rows", []))
    return typing.cast(str, rows[0]["status"]) if rows else None


async def cancelled_while_suspended(harness: HarnessEnvironment, handler: Any) -> tuple[int | None, list[str]]:
    """Start ``handler``, wait until Restate has shut its attempt down, cancel it; its HTTP status, and its statuses."""
    key = str(uuid.uuid4())
    sent = await harness.client.object_send(handler, key=key, arg=None)
    seen: list[str] = []
    async with httpx2.AsyncClient(timeout=30) as admin:
        for _ in range(100):
            now = await status(admin, harness, sent.invocation_id)
            if now is not None and (not seen or seen[-1] != now):
                seen.append(now)
            if now == "suspended":
                break
            await asyncio.sleep(0.1)
        assert seen and seen[-1] == "suspended", seen
        (await admin.patch(f"{harness.admin_api_url}/invocations/{sent.invocation_id}/cancel")).raise_for_status()
        try:
            answered = await admin.get(
                f"{harness.ingress_url}/restate/invocation/{sent.invocation_id}/attach", timeout=10
            )
            code: int | None = answered.status_code
        except httpx2.TimeoutException:
            code = None
        seen.append(f"after the cancel: {await status(admin, harness, sent.invocation_id)}")
        if code is None:
            await admin.patch(f"{harness.admin_api_url}/invocations/{sent.invocation_id}/kill")
    return code, seen


async def test_a_cancellation_reaches_an_invocation_that_was_shut_down_while_it_waited_for_a_call(
    harness: HarnessEnvironment,
):
    code, seen = await cancelled_while_suspended(harness, waits)
    assert code == 409, f"the cancelled invocation did not end in 10 s: {seen}"


async def test_a_handler_that_watches_for_its_cancellation_learns_of_it_after_it_was_shut_down(
    harness: HarnessEnvironment,
):
    code, seen = await cancelled_while_suspended(harness, waits_and_watches)
    assert code == 200, f"the cancelled invocation did not end in 10 s: {seen}"


async def test_a_cancelled_invocation_that_goes_on_waiting_is_not_resumed_over_and_over(harness: HarnessEnvironment):
    # Waiting on a cancellation that Restate has stored resumes the invocation at once. The attempt that follows
    # the cancellation knows of it, and waits for its call alone.
    key = str(uuid.uuid4())
    sent = await harness.client.object_send(waits_whatever_comes, key=key, arg=None)
    async with httpx2.AsyncClient(timeout=30) as admin:
        for _ in range(100):
            if await status(admin, harness, sent.invocation_id) == "suspended":
                break
            await asyncio.sleep(0.1)
        before = attempts.get(key, 0)
        (await admin.patch(f"{harness.admin_api_url}/invocations/{sent.invocation_id}/cancel")).raise_for_status()
        await asyncio.sleep(4)
        after_the_cancel = attempts.get(key, 0)
        await asyncio.sleep(4)
        later = attempts.get(key, 0)
        now = await status(admin, harness, sent.invocation_id)
        await admin.patch(f"{harness.admin_api_url}/invocations/{sent.invocation_id}/kill")
    # The cancellation started one attempt, which was shut down in its turn, and no more came.
    assert (after_the_cancel - before, later - after_the_cancel, now) == (1, 0, "suspended")
