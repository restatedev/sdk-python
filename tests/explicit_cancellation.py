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
"""Explicit cancellation (preview): a cancelled handler is told, and decides what to stop."""

# pylint: disable=missing-function-docstring

import asyncio
from datetime import timedelta

import httpx2
import pytest

import restate
from support import RESTATE_IMAGE  # pylint: disable=import-error
from restate import Context, HarnessEnvironment, Service, VirtualObject, Workflow
from restate._preview import cancel_previous_calls, cancellation


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


# --- Configuration ------------------------------------------------------------------------------


def test_it_is_off_by_default():
    greeter = Service("greeter")

    @greeter.handler()
    async def greet(ctx: Context, name: str) -> str:
        return name

    assert not greeter.explicit_cancellation
    assert not greeter.handlers["greet"].explicit_cancellation


def test_it_is_a_key_of_a_whole_service_or_of_one_handler():
    assert Service("greeter", explicit_cancellation=True).explicit_cancellation
    counter = VirtualObject("counter", explicit_cancellation=True)
    assert counter.explicit_cancellation

    @counter.handler(explicit_cancellation=True)
    async def add(ctx: restate.ObjectContext, n: int) -> None:
        pass

    flow = Workflow("flow")

    @flow.main(explicit_cancellation=True)
    async def run(ctx: restate.WorkflowContext) -> None:
        pass

    @flow.handler(explicit_cancellation=True)
    async def poke(ctx: restate.WorkflowSharedContext) -> None:
        pass

    assert counter.handlers["add"].explicit_cancellation
    assert flow.handlers["run"].explicit_cancellation
    assert flow.handlers["poke"].explicit_cancellation


def test_cancellation_needs_the_context_of_a_handler():
    with pytest.raises(restate.TerminalError, match="takes the context a handler is invoked with"):
        cancellation(object())  # type: ignore[arg-type]


# --- Against a Restate server -------------------------------------------------------------------

sleeper = Service("sleeper")


@sleeper.handler()
async def nap(ctx: Context) -> str:
    await ctx.sleep(timedelta(minutes=5))
    return "rested"


@sleeper.handler()
async def echo(ctx: Context, n: int) -> int:
    return n


explicit = Service("explicit", explicit_cancellation=True)


@explicit.handler()
async def wait_for_nap(ctx: Context) -> list[str]:
    """Wait for a nap; when cancelled, cancel the nap and return the ids of the calls cancelled."""
    assert cancellation(ctx) is cancellation(ctx)
    done, _ = await restate.select(nap=ctx.service_call(nap, arg=None), cancelled=cancellation(ctx))
    assert done == "cancelled"
    return await cancel_previous_calls(ctx)


on_one_handler = Service("on_one_handler")


@on_one_handler.handler(explicit_cancellation=True)
async def echo_twice(ctx: Context) -> list[str]:
    """Make two calls that complete: there is nothing left to cancel."""
    await ctx.service_call(echo, arg=1)
    await ctx.service_call(echo, arg=2)
    return await cancel_previous_calls(ctx)


implicit = Service("implicit")


@implicit.handler()
async def nap_implicitly(ctx: Context) -> None:
    await ctx.service_call(nap, arg=None)


@implicit.handler()
async def ask_for_cancellation(ctx: Context) -> None:
    await cancellation(ctx)


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        restate.app([sleeper, explicit, on_one_handler, implicit]), restate_image=RESTATE_IMAGE
    ) as harness:
        yield harness


async def cancel_once_running(harness: HarnessEnvironment, invocation_id: str) -> None:
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
        (await admin.patch(f"{harness.admin_api_url}/invocations/{invocation_id}/cancel")).raise_for_status()


async def result_of(harness: HarnessEnvironment, invocation_id: str) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=30) as ingress:
        return await ingress.get(f"{harness.ingress_url}/restate/invocation/{invocation_id}/attach")


@pytest.mark.anyio
async def test_a_cancelled_handler_goes_on_and_cancels_its_calls_itself(harness: HarnessEnvironment):
    sent = await harness.client.service_send(wait_for_nap, arg=None)
    await cancel_once_running(harness, sent.invocation_id)

    answer = await result_of(harness, sent.invocation_id)
    assert answer.status_code == 200, answer.text
    [nap_invocation] = answer.json()
    assert (await result_of(harness, nap_invocation)).status_code == 409


@pytest.mark.anyio
async def test_calls_that_completed_are_not_cancelled(harness: HarnessEnvironment):
    assert await harness.client.service_call(echo_twice, arg=None) == []


@pytest.mark.anyio
async def test_without_it_the_cancellation_fails_the_handler(harness: HarnessEnvironment):
    sent = await harness.client.service_send(nap_implicitly, arg=None)
    await cancel_once_running(harness, sent.invocation_id)
    assert (await result_of(harness, sent.invocation_id)).status_code == 409


@pytest.mark.anyio
async def test_without_it_cancellation_is_refused(harness: HarnessEnvironment):
    sent = await harness.client.service_send(ask_for_cancellation, arg=None)
    failed = await result_of(harness, sent.invocation_id)
    assert failed.status_code == 500
    assert "needs a handler with explicit_cancellation=True" in failed.text
