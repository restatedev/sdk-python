"""LangChain services used by the AI integration tests."""

from __future__ import annotations

import os
from typing import Any, cast

import restate
from langchain.agents import create_agent
from langchain_core.messages import (
    HumanMessage,
    messages_from_dict,
    messages_to_dict,
)
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, SecretStr
from restate import Context, ObjectContext, ObjectSharedContext, TerminalError
from restate.ext.langchain import RestateMiddleware, restate_context, restate_object_context

MODEL = ChatOpenAI(
    model="gpt-4o-mini",
    api_key=SecretStr(os.environ.get("OPENAI_API_KEY", "scripted-tests-replace-this-model")),
    max_retries=0,
)
LLM_RUN_OPTIONS: restate.RunOptions[Any] = restate.RunOptions(max_attempts=1)
SerializedMessages = list[dict[str, Any]]


@tool
async def get_weather(city: str) -> dict[str, str]:
    """Get the current weather for a city."""

    async def call_weather_api() -> dict[str, str]:
        return {"city": city, "weather": "sunny and 22 degrees Celsius"}

    return await restate_object_context().run_typed(f"get weather for {city}", call_weather_api)


weather_agent = create_agent(
    model=MODEL,
    tools=[get_weather],
    system_prompt=(
        "You are a concise assistant. When the user asks about weather you MUST call get_weather. "
        "Answer in one short sentence per city."
    ),
    middleware=[RestateMiddleware(run_options=LLM_RUN_OPTIONS)],
)
agent_object = restate.VirtualObject("LangChainAgentObject")


@agent_object.handler()
async def message(ctx: ObjectContext, prompt: str) -> SerializedMessages:
    stored_history = await ctx.get("messages", type_hint=list[dict[str, Any]]) or []
    history = messages_from_dict(stored_history)
    input_messages = [*history, HumanMessage(content=prompt, id=str(ctx.uuid()))]
    result = await weather_agent.ainvoke(cast(Any, {"messages": input_messages}))
    serialized_messages = messages_to_dict(result["messages"])
    ctx.set("messages", serialized_messages)
    return serialized_messages


@agent_object.handler(kind="shared")
async def get_session_messages(ctx: ObjectSharedContext) -> SerializedMessages:
    return await ctx.get("messages", type_hint=list[dict[str, Any]]) or []


@tool
async def explode(reason: str) -> dict[str, str]:
    """Process the request and always fail permanently."""
    raise TerminalError(f"tool failed permanently: {reason}")


failing_agent = create_agent(
    model=MODEL,
    tools=[explode],
    system_prompt="You MUST call explode for every request. Never answer directly.",
    middleware=[RestateMiddleware(run_options=LLM_RUN_OPTIONS)],
)
failing_service = restate.Service("LangChainFailingAgent")


@failing_service.handler()
async def failing_run(ctx: Context, prompt: str) -> str:
    await failing_agent.ainvoke({"messages": [HumanMessage(content=prompt, id=str(ctx.uuid()))]})
    return "unexpected success"


billing_agent = create_agent(
    model=MODEL,
    system_prompt="You handle billing questions. Reply in one sentence.",
    middleware=[RestateMiddleware(run_options=LLM_RUN_OPTIONS)],
)


@tool
async def handoff_to_billing(question: str) -> dict[str, str]:
    """Delegate a billing question to the local billing agent."""
    ctx = restate_context()
    result = await billing_agent.ainvoke({"messages": [HumanMessage(content=question, id=str(ctx.uuid()))]})
    return {"answer": str(result["messages"][-1].content)}


triage_agent = create_agent(
    model=MODEL,
    tools=[handoff_to_billing],
    system_prompt=(
        "For billing, invoice, refund, or payment questions you MUST call handoff_to_billing. Do not answer directly."
    ),
    middleware=[RestateMiddleware(run_options=LLM_RUN_OPTIONS)],
)
triage_service = restate.Service("LangChainTriageAgent")


@triage_service.handler()
async def triage_run(ctx: Context, question: str) -> SerializedMessages:
    result = await triage_agent.ainvoke({"messages": [HumanMessage(content=question, id=str(ctx.uuid()))]})
    return messages_to_dict(result["messages"])


class SpecialistRequest(BaseModel):
    question: str


class SpecialistReply(BaseModel):
    answer: str


specialist_agent = create_agent(
    model=MODEL,
    system_prompt="You are a database expert. Reply in one sentence.",
    middleware=[RestateMiddleware(run_options=LLM_RUN_OPTIONS)],
)
specialist_service = restate.Service("LangChainRemoteSpecialist")


@specialist_service.handler()
async def specialist_answer(ctx: Context, request: SpecialistRequest) -> SpecialistReply:
    result = await specialist_agent.ainvoke({"messages": [HumanMessage(content=request.question, id=str(ctx.uuid()))]})
    return SpecialistReply(answer=str(result["messages"][-1].content))


@tool
async def ask_specialist(question: str) -> dict[str, str]:
    """Ask the remote database specialist a question."""
    reply = await restate_context().service_call(
        specialist_answer,
        arg=SpecialistRequest(question=question),
    )
    return {"answer": reply.answer}


coordinator_agent = create_agent(
    model=MODEL,
    tools=[ask_specialist],
    system_prompt=(
        "For any database or specialist question you MUST call ask_specialist and return its answer. "
        "Do not answer directly."
    ),
    middleware=[RestateMiddleware(run_options=LLM_RUN_OPTIONS)],
)
coordinator_service = restate.Service("LangChainCoordinator")


@coordinator_service.handler()
async def coordinator_run(ctx: Context, question: str) -> SerializedMessages:
    result = await coordinator_agent.ainvoke({"messages": [HumanMessage(content=question, id=str(ctx.uuid()))]})
    return messages_to_dict(result["messages"])


def app():
    return restate.app(
        [
            agent_object,
            failing_service,
            triage_service,
            specialist_service,
            coordinator_service,
        ]
    )
