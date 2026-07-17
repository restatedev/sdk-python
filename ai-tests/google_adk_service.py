"""Google ADK services used by the AI integration tests."""

from __future__ import annotations

from typing import Any

import restate
from google.adk import Runner
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.events import Event
from google.adk.sessions import InMemorySessionService, Session
from google.genai import types
from pydantic import BaseModel
from restate import Context, ObjectContext, ObjectSharedContext, TerminalError
from restate.ext.adk import RestatePlugin, RestateSessionService, restate_context, restate_object_context

MODEL = "gemini-2.5-flash"
WEATHER_SESSION_ID = "weather"
FAILING_SESSION_ID = "failing"
TRIAGE_SESSION_ID = "triage"
LLM_RUN_OPTIONS: restate.RunOptions[Any] = restate.RunOptions(max_attempts=1)
AgentEvents = list[dict[str, Any]]


def normalized_event(event: Event) -> dict[str, Any]:
    """Keep generated ADK content while excluding random event IDs and timestamps."""
    return {
        "author": event.author,
        "content": event.content.model_dump(mode="json") if event.content else None,
    }


async def get_weather(city: str) -> dict[str, str]:
    """Get the current weather for a city."""

    async def call_weather_api() -> dict[str, str]:
        return {"city": city, "weather": "sunny and 22 degrees Celsius"}

    return await restate_object_context().run_typed(f"get weather for {city}", call_weather_api)


weather_agent = Agent(
    name="weather_agent",
    model=MODEL,
    instruction=(
        "You are a concise assistant. When the user asks about weather you MUST call get_weather. "
        "Answer in one short sentence per city."
    ),
    tools=[get_weather],
)
weather_adk_app = App(
    name="adk_weather",
    root_agent=weather_agent,
    plugins=[RestatePlugin(run_options=LLM_RUN_OPTIONS)],
)
weather_session_service = RestateSessionService()
weather_runner = Runner(app=weather_adk_app, session_service=weather_session_service)

agent_object = restate.VirtualObject("GoogleAdkAgentObject")


@agent_object.handler()
async def message(ctx: ObjectContext, prompt: str) -> AgentEvents:
    if not await weather_session_service.has_session(session_id=WEATHER_SESSION_ID):
        await weather_session_service.create_session(
            app_name=weather_adk_app.name,
            user_id=ctx.key(),
            session_id=WEATHER_SESSION_ID,
        )
    events = weather_runner.run_async(
        user_id=ctx.key(),
        session_id=WEATHER_SESSION_ID,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text=prompt)]),
    )
    return [normalized_event(event) async for event in events]


@agent_object.handler(kind="shared")
async def get_session_events(ctx: ObjectSharedContext) -> AgentEvents:
    session = await ctx.get(f"session_store::{WEATHER_SESSION_ID}", type_hint=Session)
    return [normalized_event(event) for event in session.events] if session else []


async def explode(reason: str) -> dict[str, str]:
    """Process the request and always fail permanently."""
    raise TerminalError(f"tool failed permanently: {reason}")


failing_agent = Agent(
    name="failing_agent",
    model=MODEL,
    instruction="You MUST call explode for every request. Never answer directly.",
    tools=[explode],
)
failing_adk_app = App(
    name="adk_failing",
    root_agent=failing_agent,
    plugins=[RestatePlugin(run_options=LLM_RUN_OPTIONS)],
)
failing_session_service = RestateSessionService()
failing_runner = Runner(app=failing_adk_app, session_service=failing_session_service)


@agent_object.handler()
async def failing_run(ctx: ObjectContext, prompt: str) -> str:
    if not await failing_session_service.has_session(session_id=FAILING_SESSION_ID):
        await failing_session_service.create_session(
            app_name=failing_adk_app.name,
            user_id=ctx.key(),
            session_id=FAILING_SESSION_ID,
        )
    async for _ in failing_runner.run_async(
        user_id=ctx.key(),
        session_id=FAILING_SESSION_ID,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text=prompt)]),
    ):
        pass
    return "unexpected success"


billing_agent = Agent(
    name="billing_agent",
    description="Handles billing, invoices, refunds, and payments.",
    model=MODEL,
    instruction="You handle billing questions. Reply in one sentence.",
    disallow_transfer_to_parent=True,
)
triage_agent = Agent(
    name="triage_agent",
    model=MODEL,
    instruction="Transfer every billing, invoice, refund, or payment question to the billing agent.",
    sub_agents=[billing_agent],
)
triage_adk_app = App(
    name="adk_triage",
    root_agent=triage_agent,
    plugins=[RestatePlugin(run_options=LLM_RUN_OPTIONS)],
)
triage_session_service = RestateSessionService()
triage_runner = Runner(app=triage_adk_app, session_service=triage_session_service)


@agent_object.handler()
async def triage_run(ctx: ObjectContext, question: str) -> AgentEvents:
    if not await triage_session_service.has_session(session_id=TRIAGE_SESSION_ID):
        await triage_session_service.create_session(
            app_name=triage_adk_app.name,
            user_id=ctx.key(),
            session_id=TRIAGE_SESSION_ID,
        )
    events = triage_runner.run_async(
        user_id=ctx.key(),
        session_id=TRIAGE_SESSION_ID,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text=question)]),
    )
    return [normalized_event(event) async for event in events]


class SpecialistRequest(BaseModel):
    question: str


class SpecialistReply(BaseModel):
    answer: str


specialist_agent = Agent(
    name="specialist_agent",
    model=MODEL,
    instruction="You are a database expert. Reply in one sentence.",
)
specialist_adk_app = App(
    name="adk_specialist",
    root_agent=specialist_agent,
    plugins=[RestatePlugin(run_options=LLM_RUN_OPTIONS)],
)
specialist_service = restate.Service("GoogleAdkRemoteSpecialist")


@specialist_service.handler()
async def specialist_answer(ctx: Context, request: SpecialistRequest) -> SpecialistReply:
    session_service = InMemorySessionService()
    session_id = str(ctx.uuid())
    await session_service.create_session(
        app_name=specialist_adk_app.name,
        user_id="test-user",
        session_id=session_id,
    )
    runner = Runner(app=specialist_adk_app, session_service=session_service)
    answer = ""
    async for event in runner.run_async(
        user_id="test-user",
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text=request.question)]),
    ):
        if event.is_final_response() and event.content and event.content.parts:
            answer = event.content.parts[0].text or ""
    return SpecialistReply(answer=answer)


async def ask_specialist(question: str) -> dict[str, str]:
    """Ask the remote database specialist a question."""
    reply = await restate_context().service_call(
        specialist_answer,
        arg=SpecialistRequest(question=question),
    )
    return {"answer": reply.answer}


coordinator_agent = Agent(
    name="coordinator_agent",
    model=MODEL,
    instruction=(
        "For any database or specialist question you MUST call ask_specialist and return its answer. "
        "Do not answer directly."
    ),
    tools=[ask_specialist],
)
coordinator_adk_app = App(
    name="adk_coordinator",
    root_agent=coordinator_agent,
    plugins=[RestatePlugin(run_options=LLM_RUN_OPTIONS)],
)
coordinator_service = restate.Service("GoogleAdkCoordinator")


@coordinator_service.handler()
async def coordinator_run(ctx: Context, question: str) -> AgentEvents:
    session_service = InMemorySessionService()
    session_id = str(ctx.uuid())
    await session_service.create_session(
        app_name=coordinator_adk_app.name,
        user_id="test-user",
        session_id=session_id,
    )
    runner = Runner(app=coordinator_adk_app, session_service=session_service)
    events = runner.run_async(
        user_id="test-user",
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text=question)]),
    )
    return [normalized_event(event) async for event in events]


def app():
    return restate.app(
        [
            agent_object,
            specialist_service,
            coordinator_service,
        ]
    )
