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
"""Explicit cancellation (preview): a cancellation fails no await, and the handler decides what to stop."""

# pylint: disable=missing-function-docstring

import asyncio
import typing
from datetime import timedelta

import httpx
import pytest

import restate
from restate import Context, HarnessEnvironment, Service, VirtualObject, Workflow
from restate._preview import cancel_previous_calls, cancellation, explicit_cancellation
from restate.server_context import ServerInvocationContext


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


def test_is_off_by_default():
    greeter = Service("greeter")

    @greeter.handler()
    async def greet(ctx: Context, name: str) -> str:
        return name

    assert not greeter.explicit_cancellation
    assert not greeter.handlers["greet"].explicit_cancellation


def test_applies_to_a_service_or_a_single_handler():
    counter = explicit_cancellation(VirtualObject("counter"))
    assert counter.explicit_cancellation

    flow = Workflow("flow")

    @explicit_cancellation
    @flow.main()
    async def run(ctx: restate.WorkflowContext) -> None:
        pass

    assert flow.handlers["run"].explicit_cancellation


# --- Against a real server ----------------------------------------------------------------------

sleeper = Service("sleeper")


@sleeper.handler()
async def nap(ctx: Context) -> str:
    await ctx.sleep(timedelta(minutes=5))
    return "rested"


waiter = Service("waiter")


@explicit_cancellation
@waiter.handler()
async def wait(ctx: Context) -> list[str]:
    """Waits for a nap; cancelled, it cancels the nap, and answers which calls it cancelled."""
    call = ctx.service_call(nap, arg=None)
    who, _ = await restate.select(call=call, cancelled=cancellation(ctx))
    assert who == "cancelled"
    return await cancel_previous_calls(ctx)


quick = Service("quick")


@quick.handler()
async def echo(ctx: Context, n: int) -> int:
    return n


@explicit_cancellation
@waiter.handler()
async def many(ctx: Context) -> list[int]:
    """Many calls, all completed: none is left to cancel, and the context keeps few of them."""
    for n in range(40):
        await ctx.service_call(echo, arg=n)
    kept = len(typing.cast(ServerInvocationContext, ctx).calls)
    return [kept, len(await cancel_previous_calls(ctx))]


implicit = Service("implicit")


@implicit.handler()
async def wait_implicitly(ctx: Context) -> list[str]:
    """Without explicit cancellation, the cancellation fails the await, and its nap is cancelled with it."""
    await ctx.service_call(nap, arg=None)
    return []


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        restate.app([sleeper, waiter, quick, implicit]), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


async def cancel_when_running(harness: HarnessEnvironment, invocation_id: str) -> None:
    """Cancel the invocation, once Restate runs it."""
    async with httpx.AsyncClient() as admin:
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


async def output(harness: HarnessEnvironment, invocation_id: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30) as ingress:
        return await ingress.get(f"{harness.ingress_url}/restate/invocation/{invocation_id}/attach")


@pytest.mark.anyio
async def test_a_cancelled_handler_with_explicit_cancellation_decides_what_to_stop(harness: HarnessEnvironment):
    sent = await harness.client.service_send(wait, arg=None)
    await cancel_when_running(harness, sent.invocation_id)
    answer = await output(harness, sent.invocation_id)
    # The handler went on, and answered: it cancelled its nap itself.
    assert answer.status_code == 200, answer.text
    [nap_id] = answer.json()
    assert (await output(harness, nap_id)).status_code == 409


@pytest.mark.anyio
async def test_without_it_the_cancellation_fails_the_handler(harness: HarnessEnvironment):
    sent = await harness.client.service_send(wait_implicitly, arg=None)
    await cancel_when_running(harness, sent.invocation_id)
    assert (await output(harness, sent.invocation_id)).status_code == 409


def test_cancellation_takes_a_handler_with_explicit_cancellation():
    with pytest.raises(restate.TerminalError, match="takes the context of a handler"):
        cancellation(object())  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_completed_calls_are_not_kept_nor_cancelled(harness: HarnessEnvironment):
    kept, cancelled = await harness.client.service_call(many, arg=None)
    assert kept < 40
    assert cancelled == 0
