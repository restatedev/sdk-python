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
"""The agents' Restate tools against a real Restate server: a workflow tool call run again after a crash."""

# pylint: disable=missing-function-docstring,import-error

import uuid
from datetime import timedelta

import pytest
from support import RESTATE_IMAGE

import restate
from restate import HarnessEnvironment, sessions
from restate.agents._object import AgentObject
from restate.agents._tools import ToolCall, workflow


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

payouts = restate.Workflow("tools_payouts")


@payouts.main()
async def pay(ctx: restate.WorkflowContext, amount: int) -> str:
    await ctx.sleep(timedelta(milliseconds=100))
    return f"paid {amount} for {ctx.key()}"


pay_tool = workflow(pay, description="Pay an amount out.")

runner = AgentObject("tools_runner", migrations=["CREATE TABLE crashed (call TEXT PRIMARY KEY)"])


@runner.handler
async def refund(ctx: sessions.SessionContext, amount: int) -> str:
    """Runs the workflow tool as tool call ``c1``.

    The first attempt starts the tool call's workflow, as the tool does, and crashes before its result. The retry runs
    the tool call, whose call to the workflow Restate refuses (409, already invoked): it waits for that run instead.
    """
    if await ctx.db.get("SELECT 1 FROM crashed WHERE call = 'c1'") is None:
        started = ctx.call(pay, amount, key=f"{ctx.key}-c1")
        await started.invocation_id()  # the run started
        await ctx.db.execute("INSERT INTO crashed (call) VALUES ('c1')")
        raise ValueError("crash after the workflow's run started")
    return await pay_tool.run({"input": amount}, ToolCall(ctx, "c1"))


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(restate.app([payouts, runner]), restate_image=RESTATE_IMAGE) as harness:
        yield harness


async def test_a_workflow_tool_call_run_again_after_a_crash_answers_with_the_run_it_started(
    harness: HarnessEnvironment,
):
    key = str(uuid.uuid4())
    assert await harness.client.object_call(refund, key=key, arg=30) == f"paid 30 for {key}-c1"
