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
"""Structured output (``output=``): the reply read as JSON and validated, against a real Restate server.

The path is the same for every framework; a Pydantic AI agent on a scripted model drives it.
"""

# pylint: disable=missing-function-docstring,missing-class-docstring

import uuid
from typing import Any

import httpx2
import pytest

pytest.importorskip("pydantic_ai")

from pydantic import BaseModel  # noqa: E402
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart  # noqa: E402
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

import restate  # noqa: E402
from restate import HarnessEnvironment, sessions  # noqa: E402
from restate.agents import pydantic_ai  # noqa: E402
from restate.agents._output import Wrong, parsed  # noqa: E402


class Plan(BaseModel):
    title: str
    asked: bool
    """Whether the model was asked for the schema: in its instructions, or (an agent of your own) in the prompt."""


def test_a_reply_is_read_as_json_in_a_fence_or_after_a_sentence():
    plan = Plan(title="t", asked=True)
    as_json = plan.model_dump_json()
    assert parsed(Plan, as_json) == plan
    assert parsed(Plan, f"Here it is:\n```json\n{as_json}\n```\nAnything else?") == plan
    assert parsed(Plan, f"Done. Here is the plan: {as_json}") == plan


def test_a_reply_that_is_not_the_answer_says_what_is_wrong():
    assert parsed(Plan, "  ") == Wrong("it is empty")
    assert parsed(Plan, "I made a plan.") == Wrong('it is not JSON: "I made a plan."')
    assert parsed(Plan, '{"title": 3}') == Wrong("title: Input should be a valid string; asked: Field required")


AGAIN = "Your reply is not the answer asked for"


def _prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for part in m.parts
        if isinstance(part, UserPromptPart)
    ]


def planner(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """The scripted model, by the first word of the prompt it answers (a re-ask answers the prompt before it)."""
    prompts = _prompts(messages)
    reasked = prompts[-1].startswith(AGAIN)
    text = prompts[-2] if reasked else prompts[-1]
    word, _, rest = text.partition(" ")
    title = rest.split("\n")[0]
    asked = "JSON schema" in (info.instructions or "") or "JSON schema" in text
    as_json = Plan(title=title, asked=asked).model_dump_json()
    if word == "fenced":
        return ModelResponse(parts=[TextPart(f"Sure:\n```json\n{as_json}\n```")])
    if word == "prose" and not reasked:
        return ModelResponse(parts=[TextPart("I made a plan.")])
    if word == "bad":
        return ModelResponse(parts=[TextPart('{"title": 3}')])
    return ModelResponse(parts=[TextPart(as_json)])


planning = pydantic_ai.agent("output_planner", model=FunctionModel(planner), system="Plan.", output=Plan)


@planning.handler
async def own(ctx: sessions.SessionContext, text: str) -> Plan:
    """An agent of your own keeps its instructions: the schema goes with the prompt."""
    from pydantic_ai import Agent  # pylint: disable=import-outside-toplevel

    mine: Agent[None, str] = Agent(FunctionModel(planner), instructions="Mine.")
    plan = await pydantic_ai.prompt(ctx, text, agent=mine, output=Plan)
    assert isinstance(plan, Plan)
    return plan


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        restate.app([planning]), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


async def post(harness: HarnessEnvironment, path: str, **kwargs: Any) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=60) as ingress:
        return await ingress.post(f"{harness.ingress_url}/output_planner/{path}", **kwargs)


async def chat(harness: HarnessEnvironment, key: str, text: str) -> httpx2.Response:
    return await post(harness, f"{key}/chat", content=text, headers={"content-type": "text/plain"})


def key() -> str:
    return uuid.uuid4().hex


@pytest.mark.anyio
@pytest.mark.parametrize("word", ["plan", "fenced", "prose"])
async def test_chat_answers_with_the_value_of_the_reply(harness: HarnessEnvironment, word: str):
    k = key()
    answer: Any = (await chat(harness, k, f"{word} {k}")).json()
    assert answer == {"title": k, "asked": True}


@pytest.mark.anyio
async def test_a_reply_that_is_wrong_twice_fails_the_message_and_the_next_one_is_answered(harness: HarnessEnvironment):
    k = key()
    failed = await chat(harness, k, f"bad {k}")
    assert failed.status_code == 500
    assert "the agent's reply is not the answer asked for: title: Input should be a valid string" in failed.text
    assert (await chat(harness, k, f"plan {k}")).json() == {"title": k, "asked": True}


@pytest.mark.anyio
async def test_a_prompt_of_an_agent_of_your_own_answers_with_the_value(harness: HarnessEnvironment):
    k = key()
    assert (await post(harness, f"{k}/own", json=f"plan {k}")).json() == {"title": k, "asked": True}
