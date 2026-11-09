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
"""The agents' Restate tools against a real Restate server: the workflow tool sends, then attaches."""

# pylint: disable=missing-function-docstring,import-error

import pytest
from support import RESTATE_IMAGE

import restate
from restate import sessions
from restate.agents._object import AgentObject
from restate.agents._tools import ToolCall, workflow
from restate.sessions import internal


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio, pytest.mark.server]


async def test_the_workflow_tool_answers_with_its_run_run_again_after_a_crash_too():
    greeter = restate.Workflow("tools_greeter")

    @greeter.main()
    async def hello(ctx: restate.WorkflowContext) -> str:
        return "hi"

    say_hi = workflow(hello, description="Say hi.")
    runner = AgentObject("tools_runner", migrations=["CREATE TABLE crashed (call TEXT PRIMARY KEY)"])

    @runner.handler
    async def greet(ctx: sessions.SessionContext, call_id: str) -> str:
        """Runs the workflow tool as tool call ``call_id``. Its first attempt starts the tool call's workflow, as the
        tool does, and crashes before its result: the retry runs the tool call, whose send starts nothing (the run
        started), and which waits for that run."""
        if await ctx.db.get("SELECT 1 FROM crashed WHERE call = ?", [call_id]) is None:
            await internal.start(ctx, hello, key=f"{ctx.key}-{call_id}")
            await ctx.db.execute("INSERT INTO crashed (call) VALUES (?)", [call_id])
            raise ValueError("crash after the workflow's run started")
        return await say_hi.run({}, ToolCall(ctx, call_id))

    async with restate.create_test_harness(restate.app([greeter, runner]), restate_image=RESTATE_IMAGE) as harness:
        assert await harness.client.object_call(greet, key="alice", arg="c1") == "hi"
