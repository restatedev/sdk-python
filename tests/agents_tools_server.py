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


# --- A session object that is no agent: the conversation stores, nothing publishes, waits fail for good ------------


def _scripted(tool_name: str, replies: list[str]):
    """A model that calls ``tool_name`` once, then answers with what the tool returned (kept in ``replies``)."""
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returned = [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]
        if not returned:
            return ModelResponse(parts=[ToolCallPart(tool_name, {}, tool_call_id="c1")])
        replies.append(str(returned[0].content))
        return ModelResponse(parts=[TextPart(f"the tool said: {returned[0].content}")])

    return FunctionModel(model)


async def test_an_app_of_only_a_session_object_that_prompts_answers_a_turn_of_tool_calls_and_stores_it():
    """No agent, so no companion service at all: the turn is answered and stored, and publishes nothing."""
    from restate.agents import pydantic_ai, tool  # pylint: disable=import-outside-toplevel

    @tool
    async def lookup() -> str:
        """Look it up."""
        return "found"

    replies: list[str] = []
    desk = sessions.SessionObject("plain_desk")

    @desk.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await pydantic_ai.prompt(ctx, text, model=_scripted("lookup", replies), tools=[lookup])

    @desk.handler
    async def turns(ctx: sessions.SessionContext) -> list[list[str]]:
        rows = await ctx.db.values("SELECT answer FROM restate_agent_turns")
        steps = await ctx.db.values("SELECT count(*) FROM restate_agent_items")
        return [[str(row[0]) for row in rows], [str(steps[0][0])]]

    async with restate.create_test_harness(restate.app([desk]), restate_image=RESTATE_IMAGE) as harness:
        assert await harness.client.object_call(ask, key="alice", arg="hi") == "the tool said: found"
        [answers, [steps]] = await harness.client.object_call(turns, key="alice", arg=None)
        assert answers == ["the tool said: found"] and int(steps) >= 3
    assert replies == ["found"]


async def test_a_wait_for_a_signal_from_a_session_object_that_is_no_agent_fails_once_with_what_to_do():
    """The tool's call fails with a terminal error, its result for the model, in the one attempt: not retried."""
    from restate.agents import pydantic_ai  # pylint: disable=import-outside-toplevel
    from restate.agents._tools import wait_for_signal  # pylint: disable=import-outside-toplevel

    approval = wait_for_signal("approval", description="Wait for an approval.")
    replies: list[str] = []
    attempts: list[str] = []
    desk = sessions.SessionObject("plain_waiter")

    @desk.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        attempts.append(ctx.request.id)
        return await pydantic_ai.prompt(ctx, text, model=_scripted(approval.name, replies), tools=[approval])

    async with restate.create_test_harness(restate.app([desk]), restate_image=RESTATE_IMAGE) as harness:
        answer = await harness.client.object_call(ask, key="alice", arg="refund o1")
    assert len(attempts) == 1
    assert len(replies) == 1 and replies[0].startswith("Error: waiting for signal 'approval' needs an agent")
    assert "`<framework>.agent(...)`" in replies[0] and answer == f"the tool said: {replies[0]}"
