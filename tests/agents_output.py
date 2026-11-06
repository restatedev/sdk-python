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
"""Structured output (``output=``) in each framework, on scripted models, against a real Restate server.

Each scripted model answers with a ``Plan`` whose ``mode`` says how it was
asked for one: natively (Pydantic AI's ``native`` or ``prompted`` mode, the
OpenAI Agents SDK's output type, ADK's schema or its ``set_model_response``
tool), or in the system prompt (LangGraph).
"""

# pylint: disable=missing-function-docstring,missing-class-docstring

import json
import typing
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx2
import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("agents")
pytest.importorskip("google.adk")
pytest.importorskip("langgraph")

from agents import Agent, ModelResponse as OpenAIResponse, Usage  # noqa: E402
from agents import Model as OpenAIModel  # noqa: E402
from google.adk.models import BaseLlm  # noqa: E402
from google.adk.models.llm_request import LlmRequest  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.genai import types  # noqa: E402
from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel  # noqa: E402

import restate  # noqa: E402
from restate import HarnessEnvironment, sessions  # noqa: E402
from restate.agents import adk, langgraph, openai, pydantic_ai, tool  # noqa: E402
from restate.agents._api import Messages  # noqa: E402
from restate.agents._output import Wrong, parsed  # noqa: E402


class Plan(BaseModel):
    title: str
    mode: str
    """How the model was asked for a ``Plan``."""


def test_a_reply_is_read_as_json_in_a_fence_or_after_a_sentence():
    plan = Plan(title="t", mode="m")
    as_json = plan.model_dump_json()
    assert parsed(Plan, as_json) == plan
    assert parsed(Plan, f"Here it is:\n```json\n{as_json}\n```\nAnything else?") == plan
    assert parsed(Plan, f"Done. Here is the plan: {as_json}") == plan


def test_a_reply_that_is_not_the_answer_says_what_is_wrong():
    assert parsed(Plan, "  ") == Wrong("it is empty")
    assert parsed(Plan, "I made a plan.") == Wrong('it is not JSON: "I made a plan."')
    assert parsed(Plan, '{"title": 3}') == Wrong("title: Input should be a valid string; mode: Field required")


AGAIN = "Your reply is not the answer asked for"


def _reply(prompts: list[str], mode: str, retried: bool = False) -> str:
    """The scripted reply to the prompt that ``prompts`` ends with (a re-ask is answered for the prompt before it)."""
    again = retried or prompts[-1].startswith(AGAIN)
    text = next(p for p in reversed(prompts) if not p.startswith(AGAIN))
    word, _, title = text.partition(" ")
    as_json = Plan(title=title, mode=mode).model_dump_json()
    if word == "fenced":
        return f"Sure:\n```json\n{as_json}\n```"
    if word == "prose" and not again:
        return "I made a plan."
    if word == "bad":
        return '{"title": 3}'
    return as_json


# --- Pydantic AI: NativeOutput, or PromptedOutput for a model without a JSON schema mode ---


def _pai_prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for m in messages
        if isinstance(m, ModelRequest)
        for part in m.parts
        if isinstance(part, UserPromptPart)
    ]


def pai_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    last = messages[-1]
    retried = isinstance(last, ModelRequest) and any(isinstance(p, RetryPromptPart) for p in last.parts)
    mode = info.model_request_parameters.output_mode
    return ModelResponse(parts=[TextPart(_reply(_pai_prompts(messages), mode, retried))])


pai_native = pydantic_ai.agent("pai_native", model=FunctionModel(pai_model), system="Plan.", output=Plan)
pai_prompted = pydantic_ai.agent(
    "pai_prompted",
    model=FunctionModel(pai_model, profile={"supports_json_schema_output": False}),
    system="Plan.",
    output=Plan,
)


@pai_native.handler
async def own(ctx: sessions.SessionContext, text: str) -> Plan:
    """An agent of your own, whose output is text: the run asks it for a Plan."""
    from pydantic_ai import Agent as PydanticAgent  # pylint: disable=import-outside-toplevel

    mine: PydanticAgent[None, str] = PydanticAgent(FunctionModel(pai_model), instructions="Mine.")
    plan = await pydantic_ai.prompt(ctx, text, agent=mine, output=Plan)
    assert isinstance(plan, Plan)
    return plan


# --- LangGraph: the system prompt asks ---


class LangGraphModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
        return self

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        system = next((str(m.content) for m in messages if isinstance(m, SystemMessage)), "")
        mode = "system" if "JSON schema" in system else "none"
        prompts = [str(m.content) for m in messages if isinstance(m, HumanMessage)]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(_reply(prompts, mode)))])


lg = langgraph.agent("lg_planner", model=LangGraphModel(), system="Plan.", output=Plan)


# --- The OpenAI Agents SDK: the output type of the agent the run starts with ---


def _said(text: str) -> ResponseOutputMessage:
    content: list[Any] = [ResponseOutputText(type="output_text", text=text, annotations=[])]
    return ResponseOutputMessage(id="msg", type="message", role="assistant", status="completed", content=content)


class OpenAIScripted(OpenAIModel):
    async def get_response(
        self,
        system_instructions: str | None,
        input: Any,
        model_settings: Any,
        tools: Any,
        output_schema: Any,
        *args: Any,
        **kwargs: Any,
    ) -> OpenAIResponse:
        items = typing.cast(
            list[dict[str, Any]], [{"role": "user", "content": input}] if isinstance(input, str) else input
        )
        prompts = [str(i["content"]) for i in items if i.get("role") == "user" and isinstance(i.get("content"), str)]
        handed = any(i.get("type") == "function_call" for i in items[-3:])
        if prompts[-1].startswith("handoff") and system_instructions == "You triage." and not handed:
            call = ResponseFunctionToolCall(
                type="function_call", call_id="call-h", name="transfer_to_refunds", arguments="{}"
            )
            return OpenAIResponse(output=[call], usage=Usage(), response_id=None)
        mode = "native" if output_schema is not None else "text"
        text = _reply(prompts, mode)
        if system_instructions == "You handle refunds." and not prompts[-1].startswith(AGAIN):
            text = "Refunded."  # an agent handed off to, without the output type: it answers in text
        return OpenAIResponse(output=[_said(text)], usage=Usage(), response_id=None)

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


refunds = Agent(name="refunds", instructions="You handle refunds.", model=OpenAIScripted())
oai = openai.agent("oai_planner", model=OpenAIScripted(), system="Plan.", output=Plan)
oai_desk = openai.agent(
    "oai_desk_planner",
    Agent(name="triage", instructions="You triage.", model=OpenAIScripted(), handoffs=[refunds]),
    output=Plan,
)


# --- ADK: the root agent's output_schema, through ADK's set_model_response tool when there are tools too ---


@tool
async def get_weather(city: str) -> str:
    """The current weather in a city."""
    return f"sunny in {city}"


class ADKScripted(BaseLlm):
    model: str = "scripted"

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        prompts = [
            part.text
            for content in llm_request.contents
            if content.role == "user"
            for part in content.parts or []
            if part.text
        ]
        config = llm_request.config
        if config is not None and config.response_schema is not None:
            parts = [types.Part(text=_reply(prompts, "native"))]
        elif "set_model_response" in llm_request.tools_dict:
            returned = any(
                part.function_response is not None for content in llm_request.contents for part in content.parts or []
            )
            if returned:  # ADK takes the tool's arguments as the answer, and asks nothing more
                parts = [types.Part(text="unexpected")]
            else:
                args = json.loads(_reply(prompts, "tool"))
                parts = [types.Part(function_call=types.FunctionCall(name="set_model_response", args=args))]
        else:
            parts = [types.Part(text=_reply(prompts, "none"))]
        yield LlmResponse(content=types.Content(role="model", parts=parts))


adk_native = adk.agent("adk_native", model=ADKScripted(), system="Plan.", output=Plan)
adk_tools = adk.agent("adk_tools", model=ADKScripted(), system="Plan.", tools=[get_weather], output=Plan)


# --- The server ---


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
async def harness():
    served = [pai_native, pai_prompted, lg, oai, oai_desk, adk_native, adk_tools]
    async with restate.create_test_harness(
        restate.app(served), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


async def post(harness: HarnessEnvironment, path: str, **kwargs: Any) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=60) as ingress:
        return await ingress.post(f"{harness.ingress_url}/{path}", **kwargs)


async def chat(harness: HarnessEnvironment, agent: str, text: str, key: str | None = None) -> httpx2.Response:
    key = key or uuid.uuid4().hex
    return await post(harness, f"{agent}/{key}/chat", content=text, headers={"content-type": "text/plain"})


async def user_messages(harness: HarnessEnvironment, agent: str, key: str) -> list[str]:
    page = typing.cast(Messages, (await post(harness, f"{agent}/{key}/messages", json={})).json())
    return [
        " ".join(str(part.get("text", "")) for part in message["parts"])
        for message in page["messages"]
        if message["role"] == "user"
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("agent", "mode"),
    [
        ("pai_native", "native"),
        ("pai_prompted", "prompted"),
        ("lg_planner", "system"),
        ("oai_planner", "native"),
        ("adk_native", "native"),
        ("adk_tools", "tool"),
    ],
)
async def test_chat_answers_with_a_plan_asked_for_natively_where_the_framework_can(
    harness: HarnessEnvironment, agent: str, mode: str
):
    key = uuid.uuid4().hex
    assert (await chat(harness, agent, f"plan {key}", key)).json() == {"title": key, "mode": mode}
    # Nothing is added to the user's message.
    assert await user_messages(harness, agent, key) == [f"plan {key}"]


@pytest.mark.anyio
@pytest.mark.parametrize("agent", ["pai_native", "lg_planner"])
async def test_a_fenced_reply_is_the_answer(harness: HarnessEnvironment, agent: str):
    answer = (await chat(harness, agent, "fenced trip")).json()
    assert answer["title"] == "trip"


@pytest.mark.anyio
async def test_pydantic_ai_asks_again_for_a_reply_in_prose_itself(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    assert (await chat(harness, "pai_native", f"prose {key}", key)).json() == {"title": key, "mode": "native"}
    assert await user_messages(harness, "pai_native", key) == [f"prose {key}"]


@pytest.mark.anyio
async def test_langgraph_asks_again_for_a_reply_in_prose_with_the_schema(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    assert (await chat(harness, "lg_planner", f"prose {key}", key)).json() == {"title": key, "mode": "system"}
    first, again = await user_messages(harness, "lg_planner", key)
    assert first == f"prose {key}"
    assert again.startswith(f"{AGAIN}: it is not JSON") and '"title"' in again


@pytest.mark.anyio
async def test_an_agent_handed_off_to_that_answers_in_text_is_asked_again(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    assert (await chat(harness, "oai_desk_planner", f"handoff {key}", key)).json() == {"title": key, "mode": "text"}


@pytest.mark.anyio
@pytest.mark.parametrize("agent", ["pai_native", "lg_planner"])
async def test_a_reply_that_is_wrong_after_asking_again_fails_the_message_and_the_next_one_is_answered(
    harness: HarnessEnvironment, agent: str
):
    key = uuid.uuid4().hex
    failed = await chat(harness, agent, f"bad {key}", key)
    assert failed.status_code == 500
    assert "the agent's reply is not the answer asked for: title: Input should be a valid string" in failed.text
    assert (await chat(harness, agent, f"plan {key}", key)).json()["title"] == key


@pytest.mark.anyio
async def test_a_prompt_of_an_agent_of_your_own_answers_with_a_plan(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    assert (await post(harness, f"pai_native/{key}/own", json=f"plan {key}")).json() == {"title": key, "mode": "native"}
