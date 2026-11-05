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
# ADK leaves some of its signatures untyped (BaseTool.__init__, LlmAgent.tools).
# pyright: reportUnknownMemberType=false
"""Google ADK agents whose sessions are stored in the object's database, one event at a time.

ADK's ``Runner`` runs the agent, as a resumable app, on a session whose
events are the shared conversation of ``restate.agents`` (``_conversation.py``):
the session service stores each event as ADK appends it, and the conversation
answers each prompt once and closes a stopped, refused or failed run. The
handler is not replayed: a retried attempt (after a crash or a lost
connection) runs again from the start, and ADK resumes the invocation from
its stored events:

- ADK appends the model's tool calls before they run, and their results
  before the next request. A crash while tools are running resumes them with
  the same call ids (ADK's resumption runs a cut-off reply's calls rather than
  asking the model again), which a tool can pass on to stay idempotent.
- A tool's error fails the attempt, and Restate retries it. A
  ``restate.TerminalError``, which a retry would only repeat, is instead the
  tool's answer to the model, and so is a call to a tool the agent does not
  have (which ADK fails the run for).
- A transfer to a sub-agent holds for the next messages, as ADK's sessions do.
"""

from __future__ import annotations

import json
import time
import typing
from collections.abc import Callable, Sequence
from typing import Any, Unpack

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import new_invocation_context_id
from google.adk.apps import App, ResumabilityConfig
from google.adk.events import Event
from google.adk.models import BaseLlm
from google.adk.plugins import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, Session
from google.adk.sessions.base_session_service import GetSessionConfig, ListSessionsResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from restate.agents._common import (
    MODEL_ENV,
    ModelError,
    PerSession,
    check_own,
    configured,
    from_tool,
    model_of,
    provider_of,
)
from restate.agents._conversation import Call, Conversation, Item, Parsed, Result, Run, answer_once
from restate.agents._object import AgentObject, AgentOptions
from restate.agents._tools import Tool, current_call, tools_in
from restate.agents._turn import tool_call_ended, tool_call_started
from restate.exceptions import TerminalError
from restate.sessions import SessionContext

AnyModel = str | BaseLlm
"""A model: ``"google/<model-id>"`` (a Gemini model), or a model of ADK (``LiteLlm(...)`` for others)."""

AnyTool = Tool | BaseTool | Callable[..., Any]
"""A tool: one made with ``restate.agents`` (``tool``, ``call``, ``wait_for_signal``, ...), or ADK's (functions too)."""

_USER = "restate"
"""The user of every session: the conversation is the object's key."""

_APP = "restate"
"""The name of the app that runs an agent."""


# --- ADK's events ---------------------------------------------------------------------------------


def _text(event: Event) -> str:
    """Return the text of an event's content, without its thoughts."""
    parts = event.content.parts if event.content and event.content.parts else []
    return "".join(part.text for part in parts if part.text and not part.thought)


class _Items:
    """ADK's events, as the conversation keeps them (stored as JSON); a run's closing events are its root agent's."""

    def dump(self, item: Item) -> str:
        return typing.cast(Event, item).model_dump_json(exclude_none=True)

    def load(self, stored: str) -> Item:
        return Event.model_validate_json(stored)

    def __init__(self, author: str = "") -> None:
        self.author = author

    def prompt(self, text: str) -> Item:
        # A prompt starts an invocation of its own, which every attempt resumes.
        content = types.Content(role="user", parts=[types.Part(text=text)])
        return Event(author="user", invocation_id=new_invocation_context_id(), content=content)

    def reply(self, text: str) -> Item:
        content = types.Content(role="model", parts=[types.Part(text=text)])
        return Event(author=self.author, content=content)

    def results(self, results: Sequence[tuple[Call, str]]) -> list[Item]:
        if not results:
            return []
        parts = [
            types.Part(function_response=types.FunctionResponse(id=call.id, name=call.name, response={"result": text}))
            for call, text in results
        ]
        return [Event(author=self.author, content=types.Content(role="user", parts=parts))]

    def read(self, item: Item) -> Parsed:
        event = typing.cast(Event, item)
        text = _text(event)
        calls = [Call(call.id or "", call.name or "", call.args) for call in event.get_function_calls()]
        results: list[Result] = []
        for response in event.get_function_responses():
            output = (response.response or {}).get("result", response.response)
            results.append(Result(response.id or "", output if isinstance(output, str) else json.dumps(output)))
        said = (("user" if event.author == "user" else "assistant"), text) if text else None
        return Parsed(said, calls, results)


# --- The session ----------------------------------------------------------------------------------


class _Sessions(BaseSessionService):
    """ADK's sessions, of which there is one: the conversation, an event per item."""

    def __init__(self, conversation: Conversation) -> None:
        super().__init__()
        self.conversation = conversation

    def session(self) -> Session:
        session = Session(id=_USER, app_name=_APP, user_id=_USER, last_update_time=time.time())
        for entry in self.conversation.entries:
            event = typing.cast(Event, entry.item)
            self._update_session_state(session, event)
            session.events.append(event)
        return session

    async def create_session(
        self, *, app_name: str, user_id: str, state: dict[str, Any] | None = None, session_id: str | None = None
    ) -> Session:
        return self.session()

    async def get_session(
        self, *, app_name: str, user_id: str, session_id: str, config: GetSessionConfig | None = None
    ) -> Session | None:
        return self.session()

    async def list_sessions(self, *, app_name: str, user_id: str | None = None) -> ListSessionsResponse:
        return ListSessionsResponse(sessions=[self.session()])

    async def delete_session(self, *, app_name: str, user_id: str, session_id: str) -> None:
        raise TerminalError("an agent's session is its conversation: clear the agent instead")

    async def append_event(self, session: Session, event: Event) -> Event:
        if event.partial or event.error_code:
            # Not stored: a partial event is followed by its whole, and an error code fails the prompt (see ``run``).
            return event
        event = await super().append_event(session, event)
        if event.author != "user" and event.content and not event.get_function_responses():
            await self.conversation.append_reply(event, agent=event.author)
        else:
            await self.conversation.append(event)
        return event


class _Restate(BasePlugin):
    """Publish each tool call as it starts and ends, and mark a tool's error as the tool's."""

    def __init__(self, ctx: SessionContext) -> None:
        super().__init__(name="restate")
        self.ctx = ctx

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> None:
        await tool_call_started(self.ctx)

    async def after_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext, result: dict[str, Any]
    ) -> None:
        await tool_call_ended(self.ctx)

    async def on_tool_error_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext, error: Exception
    ) -> dict[str, Any] | None:
        if type(tool) is BaseTool:
            # A tool the agent does not have (ADK stands in a bare BaseTool for it): the model's to fix. Failing
            # the attempt would not help, as the retry resumes the same call.
            return {"result": f"Error: there is no tool named {tool.name!r}"}
        # Never mistaken for the model provider refusing; the attempt fails with it.
        from_tool(self.ctx, error)
        return None


# --- Tools ----------------------------------------------------------------------------------------


class _AdkTool(BaseTool):
    """A ``restate.agents`` tool as ADK's, which gets its call's id and validates its own arguments."""

    def __init__(self, tool: Tool) -> None:
        super().__init__(name=tool.name, description=tool.description)
        self.tool = tool

    def _get_declaration(self) -> types.FunctionDeclaration:
        return types.FunctionDeclaration(
            name=self.name, description=self.description, parameters_json_schema=self.tool.input_schema
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: ToolContext) -> Any:
        return await self.tool.run(args, current_call(tool_context.function_call_id or ""))


def tools(given: Sequence[AnyTool]) -> list[Any]:
    """Return ``given`` as tools of ADK, for an agent of your own and its sub-agents::

        refunds = LlmAgent(name="refunds", model="gemini-...", tools=adk.tools([call(refund)]))

    Args:
        given: ``restate.agents`` tools, and ADK's tools and functions (passed through).

    Returns:
        The tools, as ADK takes them.
    """
    return [_AdkTool(t) if isinstance(t, Tool) else t for t in given]


def _model(model: AnyModel | None) -> str | BaseLlm:
    """Return a model of ADK, or the model id of ``google/<model-id>``.

    Raises:
        ModelError: If ``model`` names another provider, or there is none.
    """
    if isinstance(model, BaseLlm):
        return model
    named = model_of(model, fallback="")
    if not named:
        raise ModelError(
            f"an ADK agent needs a model: model='google/<model-id>', an ADK model (LiteLlm(...)), or {MODEL_ENV}"
        )
    provider, model_id = provider_of(named)
    if provider not in ("google", "") or not model_id:
        raise ModelError(f'the model "{named}" is not google/<model-id>: pass an ADK model (LiteLlm(...)) for another')
    return model_id


# --- A run ----------------------------------------------------------------------------------------


def _app(
    ctx: SessionContext,
    own: BaseAgent | App | None,
    model: AnyModel | None,
    system: str | None,
    given: Sequence[AnyTool],
) -> App:
    """Return the app that runs the agent: resumable, with this package's plugin."""
    if own is None:
        root: BaseAgent = LlmAgent(name="assistant", model=_model(model), instruction=system or "", tools=tools(given))
        app = App(name=_APP, root_agent=root)
    else:
        app = own if isinstance(own, App) else App(name=_APP, root_agent=own)
        if given or model is not None:
            if not isinstance(app.root_agent, LlmAgent):
                raise TypeError("model and tools apply to an LlmAgent of your own, not to another agent")
            update: dict[str, Any] = {}
            if given:
                update["tools"] = [*app.root_agent.tools, *tools(given)]
            if model is not None:
                update["model"] = _model(model)
            # A clone, which also re-parents the sub-agents: those without a model of their own take the root's.
            app = app.model_copy(update={"root_agent": app.root_agent.clone(update=update)})
    resumable = ResumabilityConfig(is_resumable=True)
    return app.model_copy(
        update={"name": _APP, "plugins": [*app.plugins, _Restate(ctx)], "resumability_config": resumable}
    )


def _final(conversation: Conversation, root: BaseAgent) -> str | None:
    """Return the answer of the run that the conversation ends with, if its events already hold it.

    That is the case once the root agent is done (a resumable app stores an
    event marking it, after which ADK would end the invocation at once), and
    once an agent answered with no workflow agent above it to go on: then the
    final response is the answer. A step of a workflow (a ``SequentialAgent``
    say) is not: ADK resumes the workflow's next step.
    """
    running = conversation.running()
    if running is None:
        return None
    invocation = typing.cast(Event, running.item).invocation_id
    events = [typing.cast(Event, e.item) for e in conversation.entries if e.n > running.n]
    events = [event for event in events if event.invocation_id == invocation]
    responses = [event for event in events if _is_final(event)]
    if any(event.author == root.name and event.actions.end_of_agent for event in events):
        return _text(responses[-1]) if responses else ""
    last = next((event for event in reversed(events) if event.content is not None), None)
    if last is None or not _is_final(last) or not _answers(root, last.author):
        return None
    return _text(last) or None


def _is_final(event: Event) -> bool:
    """Return whether ``event`` is an agent's final response: content, and no tool calls."""
    return event.author != "user" and event.content is not None and event.is_final_response()


def _answers(root: BaseAgent, name: str) -> bool:
    """Return whether agent ``name``'s final response ends its run: no workflow agent above it goes on."""
    agent = root.find_agent(name)
    while agent is not None:
        if not isinstance(agent, LlmAgent):
            return False
        agent = agent.parent_agent
    return True


def _run(ctx: SessionContext, app: App) -> Run:
    """Return the run of a prompt: ADK's runner, resuming the prompt's invocation."""

    async def run(conversation: Conversation) -> str:
        if (final := _final(conversation, app.root_agent)) is not None:
            # The run had stored its answer's events, but not its answer: that is the answer, rather than ask again.
            return final
        running = conversation.running()
        invocation = typing.cast(Event, running.item).invocation_id if running is not None else None
        runner = Runner(app=app, session_service=_Sessions(conversation))
        answer = ""
        with tools_in(ctx):
            async for event in runner.run_async(user_id=_USER, session_id=_USER, invocation_id=invocation):
                if event.error_code:
                    # The model would not answer (a safety block, say), as it would not again: the prompt fails.
                    raise TerminalError(f"the model returned {event.error_code}: {event.error_message or ''}")
                if _is_final(event) and (text := _text(event)):
                    answer = text
        return answer

    return run


# --- The API --------------------------------------------------------------------------------------


async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    agent: BaseAgent | App | None = None,
    model: AnyModel | None = None,
    system: str | None = None,
    tools: Sequence[AnyTool] = (),
) -> str:
    """Answer a prompt with an ADK agent, exactly once.

    For a handler of your own; the conversation is the object's key, shared
    with its ``chat``. A retried attempt resumes the same prompt rather than
    asking again, and each prompt of an invocation is answered on its own::

        @assistant.handler
        async def summarize(ctx: sessions.SessionContext, topic: str) -> str:
            return await adk.prompt(ctx, f"Summarize what we said about {topic}")

    Args:
        ctx: The handler's context.
        text: The prompt.
        agent: An agent of your own (with sub-agents, callbacks...), or an
            ``App`` (with plugins). Defaults to an ``LlmAgent`` with ``system``
            as its instruction.
        model: ``"google/<model-id>"``, or a model of ADK. Defaults to
            ``agent``'s own, else ``$RESTATE_AGENT_MODEL`` if it is Google's.
        system: The instruction, when there is no ``agent``.
        tools: ``restate.agents`` tools, or ADK's, beside ``agent``'s own.
            For its sub-agents, see ``tools()``.

    Returns:
        The text of the agent's final response.

    Raises:
        TypeError: If both ``agent`` and ``system`` are given.
    """
    check_own(agent, system)
    app = _app(ctx, agent, model, system, tools)
    return await answer_once(ctx, text, _Items(app.root_agent.name), _run(ctx, app))


def agent(
    name: str,
    adk_agent: BaseAgent | App | None = None,
    /,
    *,
    model: PerSession[AnyModel | None] = None,
    system: PerSession[str | None] = None,
    tools: PerSession[Sequence[AnyTool]] = (),
    **options: Unpack[AgentOptions],
) -> AgentObject:
    """Create an ADK agent: a virtual object with one conversation per key.

    Its ``chat`` handler answers each message with ADK's ``Runner``; the
    session is kept in the key's database, an event at a time. Add handlers
    of your own with ``@assistant.handler``::

        assistant = adk.agent("assistant", model="google/<model-id>", system="Answer briefly.", tools=[get_weather])

    Or bring an agent of your own, or an ``App``::

        assistant = adk.agent("desk", LlmAgent(name="triage", model="...", sub_agents=[refunds]))

    ``model``, ``system`` and ``tools`` may each be a function of the
    invocation's context, sync or async, for values that differ per user.

    Args:
        name: The object's name, as Restate serves it.
        adk_agent: An agent of your own, or an ``App``. Defaults to an
            ``LlmAgent`` with ``system`` as its instruction.
        model: ``"google/<model-id>"``, or a model of ADK (``LiteLlm(...)``
            for other providers). Defaults to ``adk_agent``'s own, else
            ``$RESTATE_AGENT_MODEL`` if it is Google's.
        system: The instruction, when there is no ``adk_agent``.
        tools: ``restate.agents`` tools, or ADK's.
        **options: The object's options, such as ``description``,
            ``background`` or ``invocation_retry_policy``: see ``AgentOptions``.

    Returns:
        The agent, to serve with ``restate.serve(sessions.app(agent))``, or any ASGI server.

    Raises:
        TypeError: If both ``adk_agent`` and ``system`` are given.
        TerminalError: If there is no model, or ``model`` names a provider other than Google.
    """
    check_own(adk_agent, system)
    if adk_agent is None and (model is None or isinstance(model, str)):
        _model(model)
    obj = AgentObject(name, **options)

    async def answer(ctx: SessionContext, text: str) -> str:
        return await prompt(
            ctx,
            text,
            agent=adk_agent,
            model=await configured(model, ctx),
            system=await configured(system, ctx),
            tools=await configured(tools, ctx),
        )

    obj.chat(answer, items=_Items())
    return obj
