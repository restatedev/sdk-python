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
"""LangChain's ``create_agent`` as a Restate agent, with this package's tools, against a real Restate server.

The model is scripted: it answers from the last message.
"""

# pylint: disable=missing-function-docstring,missing-class-docstring

import collections
import typing
import uuid
from typing import Any

import httpx2
import pytest

pytest.importorskip("langchain.agents")

from langchain.agents import create_agent  # noqa: E402
from langchain.agents.structured_output import ToolStrategy  # noqa: E402
from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import restate  # noqa: E402
from restate import HarnessEnvironment  # noqa: E402
from restate.agents import call, langgraph  # noqa: E402

ledger = restate.VirtualObject("ca_ledger")
added: collections.Counter[str] = collections.Counter()


@ledger.handler()
async def add(ctx: restate.ObjectContext, amount: int) -> str:
    added[ctx.key()] += amount
    return f"{ctx.key()} owes ${added[ctx.key()]}"


class Plan(BaseModel):
    title: str


class Scripted(BaseChatModel):
    """Asks the ledger for "refund <n>", answers with the tool's result, and plans with the Plan tool for "plan <t>"."""

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        last = messages[-1]
        if isinstance(last, ToolMessage):
            reply = AIMessage(f"done: {last.text}")
        else:
            word, _, rest = str(typing.cast(HumanMessage, last).text).partition(" ")
            if word == "refund":
                reply = AIMessage(
                    "", tool_calls=[{"id": f"c-{uuid.uuid4().hex}", "name": "add", "args": {"input": int(rest)}}]
                )
            elif word == "plan":
                reply = AIMessage(
                    "", tool_calls=[{"id": f"p-{uuid.uuid4().hex}", "name": "Plan", "args": {"title": rest}}]
                )
            else:
                reply = AIMessage(f"echo: {last.text}")
        return ChatResult(generations=[ChatGeneration(message=reply)])


tools = langgraph.tools([call(add, name="add", description="Add to the ledger.", key=lambda ctx: ctx.key)])
desk = langgraph.agent("ca_desk", graph=create_agent(Scripted(), tools=tools))
planner = langgraph.agent("ca_planner", graph=create_agent(Scripted(), response_format=ToolStrategy(Plan)), output=Plan)
plain = langgraph.agent("ca_plain", graph=create_agent(Scripted(), response_format=ToolStrategy(Plan)))


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        restate.app([ledger, desk, planner, plain]), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


pytestmark = [pytest.mark.anyio]


async def chat(harness: HarnessEnvironment, agent: str, key: str, text: str) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=60) as ingress:
        return await ingress.post(
            f"{harness.ingress_url}/{agent}/{key}/chat", content=text, headers={"content-type": "text/plain"}
        )


async def test_create_agent_calls_restate_through_the_tools(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    assert (await chat(harness, "ca_desk", key, "refund 30")).json() == f"done: {key} owes $30"
    assert (await chat(harness, "ca_desk", key, "refund 5")).json() == f"done: {key} owes $35"
    assert added[key] == 35


async def test_create_agent_answers_with_its_structured_response(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    assert (await chat(harness, "ca_planner", key, f"plan {key}")).json() == {"title": key}


async def test_a_structured_response_is_the_answer_only_of_the_run_that_wrote_it(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    assert (await chat(harness, "ca_plain", key, "plan a")).json() == '{"title": "a"}'
    # The same structured response again is the answer again: the run wrote it.
    assert (await chat(harness, "ca_plain", key, "plan a")).json() == '{"title": "a"}'
    # A run that writes none answers with its text, not with the one an earlier run left in the state.
    assert (await chat(harness, "ca_plain", key, "hi")).json() == "echo: hi"
