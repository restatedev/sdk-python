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
# LangGraph's graph API (langgraph.graph, its StateGraph and compiled graphs) is untyped: no py.typed.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
"""
LangGraph agents: a tool-calling graph whose checkpoints are in the object's
database (``_checkpointer.py``). As sdk-sessions' ``langgraph/index.ts``.

The graph is the usual tool-calling loop: a model node, a tools node, and back
to the model until it answers. The handler is not replayed: a retried attempt
(a crash, a lost connection) runs again from the start, and LangGraph carries
on from the last step it stored:

- Each step is stored before the next starts (``durability="sync"``), in order
  with everything else the handler does. So the tool calls a model made are
  stored before any tool runs, and a crash during the tools resumes them with
  the same tool call ids, which a tool passes on to stay idempotent.
- A tool's error fails the attempt, which Restate retries (LangGraph's
  ToolNode would hand it to the model as the tool's result); a call of a tool
  the agent lacks, or with arguments that do not fit, is the model's to fix.
- A message is answered once per invocation: the graph records each answer in
  its state, by prompt id, and a retried attempt returns it.
"""

from __future__ import annotations

import importlib
import os
import typing
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import PrivateAttr

from restate.agents._common import (
    CANCELLED,
    UNANSWERED,
    Configure,
    ModelError,
    configured,
    from_tool,
    model_of,
    next_prompt_id,
    provider_of,
    refusal_of,
    stored_answer,
)
from restate.agents._object import RETRIES, AgentObject
from restate.agents._tools import Tool, ToolCall
from restate.agents._transcript import Transcript
from restate.agents._turn import AttemptEnded, stop_signal_of, tool_call_ended, tool_call_started, transcribing
from restate.agents.langgraph._checkpointer import SqlCheckpointSaver
from restate.exceptions import TerminalError
from restate.retry_policy import InvocationRetryPolicy
from restate.sessions import SessionContext, Step

_CANCELLATION = 409
"""The status of Restate's cancellation, as a turn that was stopped fails with it."""


def _merged(old: dict[str, str], new: dict[str, str]) -> dict[str, str]:
    return {**old, **new}


class State(TypedDict, total=False):
    """The agent's graph's state: its conversation, the prompt it answers, and each prompt's answer."""

    messages: Annotated[list[AnyMessage], add_messages]
    prompt: str
    """The id of the prompt being answered."""
    answers: Annotated[dict[str, str], _merged]
    """The answer to each prompt, by id."""


@dataclass(frozen=True)
class Options:
    """
    A LangGraph agent's options. ``model``: ``openai/model-id`` or
    ``anthropic/model-id`` (MODEL, or else ``openai/gpt-6-sol``), or a
    LangChain chat model that calls tools. ``system``: the system prompt.
    ``tools``: ``restate.agents`` tools (``tool``, ``call``, ``signal``...), or
    LangChain's.
    """

    model: str | BaseChatModel | None = None
    system: str | None = None
    tools: Sequence[Tool | BaseTool] = ()


_PROVIDERS = {
    "openai": ("langchain_openai", "ChatOpenAI", "langchain-openai"),
    "anthropic": ("langchain_anthropic", "ChatAnthropic", "langchain-anthropic"),
}
"""The providers the usual graph takes by name: LangChain's integration of each, its class, and its package."""


def _provider_of_model(named: str) -> tuple[str, str]:
    """``provider/model-id`` as its provider and model id, for a provider the usual graph takes."""
    provider, model_id = provider_of(named)
    if provider not in _PROVIDERS or not model_id:
        from_env = " (from MODEL)" if os.environ.get("MODEL") == named else ""
        raise ModelError(f'the model "{named}"{from_env} is neither openai/<model-id> nor anthropic/<model-id>')
    return provider, model_id


def _chat_model(named: str) -> BaseChatModel:
    """
    The chat model ``provider/model-id`` names, through LangChain's
    integration of the provider, imported when a model of it is used: only
    that one need be installed, beside the ``langgraph`` extra.
    """
    provider, model_id = _provider_of_model(named)
    module, class_name, package = _PROVIDERS[provider]
    try:
        integration = importlib.import_module(module)
    except ImportError as error:
        raise ModelError(
            f"{provider}/<model-id> models need {package}: install it beside restate-sdk[langgraph]"
        ) from error
    return typing.cast(BaseChatModel, getattr(integration, class_name)(model=model_id))


class _RestateTool(BaseTool):
    """A ``restate.agents`` tool as a LangChain tool: it gets its tool call's id, and checks the model's arguments itself."""

    _tool: Tool = PrivateAttr()
    _ctx: SessionContext = PrivateAttr()

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(f"{self.name} runs asynchronously only")

    async def ainvoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> ToolMessage:
        call = typing.cast(dict[str, Any], input)
        call_id = call.get("id") if isinstance(input, dict) else None
        if not call_id:
            raise ValueError(f"{self.name}: a tool call without an id")
        text = await self._tool.run(call.get("args", {}), ToolCall(self._ctx, str(call_id)))
        return ToolMessage(content=text, tool_call_id=str(call_id), name=self.name)


def _native(tool: Tool, ctx: SessionContext) -> BaseTool:
    native = _RestateTool(name=tool.name, description=tool.description, args_schema=tool.input_schema)
    native._tool = tool  # pyright: ignore[reportPrivateUsage]
    native._ctx = ctx  # pyright: ignore[reportPrivateUsage]
    return native


def _tools_node(ctx: SessionContext, tools: list[BaseTool]) -> Callable[[State, RunnableConfig], Awaitable[Any]]:
    """
    The tools node: each tool call of the model's last message, run by a
    ToolNode, which answers the model with what is wrong when a call names a
    tool the agent lacks or arguments that do not fit; any other error of a
    tool fails the attempt. The round is published as it starts and ends, for
    the clients that follow the conversation.
    """
    node = ToolNode(tools)

    async def run_tools(state: State, config: RunnableConfig) -> Any:
        await tool_call_started(ctx)
        try:
            ran = await node.ainvoke(state, config)
        except Exception as error:
            # A tool's error, never taken for the model's provider refusing.
            from_tool(ctx, error)
            raise
        await tool_call_ended(ctx)
        return ran

    return run_tools


def _text(message: BaseMessage) -> str:
    return str(message.text)


def _graph(ctx: SessionContext, model: BaseChatModel, tools: list[BaseTool], system: str | None) -> Any:
    """The agent's graph: ``model``, calling ``tools``, until it answers the prompt."""
    bound = model.bind_tools(tools) if tools else model

    async def call_model(state: State) -> dict[str, Any]:
        messages = state.get("messages", [])
        reply = await bound.ainvoke([SystemMessage(system), *messages] if system else messages)
        return {"messages": [reply]}

    def answer(state: State) -> dict[str, Any]:
        messages = state.get("messages", [])
        return {"answers": {state.get("prompt", ""): _text(messages[-1]) if messages else ""}}

    graph = StateGraph(State)
    graph.add_node("model", call_model)
    graph.add_node("tools", typing.cast(Any, _tools_node(ctx, tools)))
    graph.add_node("answer", answer)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", tools_condition, {"tools": "tools", END: "answer"})
    graph.add_edge("tools", "model")
    graph.add_edge("answer", END)
    return graph


Compiled = CompiledStateGraph[Any, Any, Any, Any]


async def _answer_once(ctx: SessionContext, text: str, options: Options) -> str:
    model = options.model if isinstance(options.model, BaseChatModel) else _chat_model(model_of(options.model))
    tools = [_native(t, ctx) if isinstance(t, Tool) else t for t in options.tools]
    # The latest checkpoint is all an agent resumes from: earlier ones would store the conversation again at each step.
    app: Compiled = _graph(ctx, model, tools, options.system).compile(
        checkpointer=SqlCheckpointSaver(ctx.db, history="latest")
    )
    config: RunnableConfig = {"configurable": {"thread_id": ctx.key}}
    prompt_id = next_prompt_id(ctx)
    while True:
        values = typing.cast(State, (await app.aget_state(config)).values)
        answers = values.get("answers", {})
        if prompt_id in answers:
            return stored_answer(answers[prompt_id])
        # Finish an interrupted run first (this prompt's, after a crash), then start this prompt's.
        # A run is unfinished while its prompt has no answer.
        last = values.get("prompt")
        interrupted = last is not None and last not in answers
        given: State | None = None if interrupted else {"messages": [HumanMessage(text)], "prompt": prompt_id}
        stop = stop_signal_of(ctx)
        run = app.ainvoke(given, config, durability="sync")
        try:
            await (stop.guard(run) if stop is not None else run)
        except AttemptEnded:
            # The attempt ended: it stops, and the next attempt goes on with the run.
            raise
        except TerminalError as error:
            # Cancelled: the run ends, and the next prompt starts afresh.
            if error.status_code == _CANCELLATION:
                await _close(app, config, CANCELLED)
                raise
            # Failed for good (a LangChain tool's terminal error): the run ends with it, rather than the next
            # prompt meeting it again. An earlier prompt's, this one goes on.
            await _close(app, config, f"Error: {error.message}")
            if not interrupted or last == prompt_id:
                raise
        except Exception as error:
            # Refused for good (a wrong API key): the run ends unanswered, and fails as terminal.
            # An earlier prompt's, this one goes on.
            refused = refusal_of(ctx, error)
            if refused is None:
                raise
            await _close(app, config, UNANSWERED)
            if not interrupted or last == prompt_id:
                raise refused from error


async def _close(app: Compiled, config: RunnableConfig, text: str) -> None:
    """
    End the run of ``app`` that was cancelled, refused or failed, as its ``answer``
    node: a result of ``text`` for each tool call of the last reply that has
    none, then ``text`` as the reply and the prompt's answer. A run that
    stored no input yet (cancelled at once) has nothing to end: the prompt the
    state has is an earlier one, which has its answer.
    """
    values = typing.cast(State, (await app.aget_state(config)).values)
    prompt = values.get("prompt")
    if prompt is None or prompt in values.get("answers", {}):
        return
    messages = values.get("messages", [])
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    last = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    calls = [c for c in (last.tool_calls if last else []) if c.get("id") and c.get("id") not in answered]
    closing: list[BaseMessage] = [
        *(ToolMessage(text, tool_call_id=str(c.get("id")), status="error") for c in calls),
        AIMessage(text),
    ]
    await app.aupdate_state(config, {"messages": closing, "answers": {prompt: text}}, as_node="answer")


async def _transcript(ctx: SessionContext) -> Transcript:
    """The thread (the object's key) from its last checkpoint, as ``messages`` gives it."""
    transcript = Transcript()
    saved = await SqlCheckpointSaver(ctx.db).aget_tuple({"configurable": {"thread_id": ctx.key}})
    messages: list[BaseMessage] = saved.checkpoint["channel_values"].get("messages", []) if saved else []
    for m in messages:
        if isinstance(m, HumanMessage):
            transcript.say("user", _text(m))
        elif isinstance(m, AIMessage):
            transcript.say("assistant", _text(m))
            for call in m.tool_calls:
                transcript.call(call.get("id") or "", call["name"], call["args"])
        elif isinstance(m, ToolMessage):
            transcript.result(m.tool_call_id, _text(m), m.status == "error")
    return transcript


async def prompt(ctx: SessionContext, text: str, options: Options | None = None) -> str:
    """
    Answer ``text`` with a LangGraph agent of ``options``, once: a retried
    attempt resumes the same prompt. Each prompt of an invocation is a prompt
    of its own. The thread is the object's key. In a handler of your own::

        @assistant.handler
        async def summarize(ctx: sessions.SessionContext, topic: str) -> str:
            return await langgraph.prompt(ctx, f"Summarize what we said about {topic}")
    """
    chosen = options or Options()

    async def read() -> Transcript:
        return await _transcript(ctx)

    async def work() -> str:
        return await _answer_once(ctx, text, chosen)

    return await transcribing(ctx, read, work)


def chat(options: Configure[Options] | None = None) -> Callable[[SessionContext, str], Awaitable[str]]:
    """
    A chat turn for ``AgentObject.chat``: each message answered by ``prompt``
    with ``options``, or the options a function of the invocation's context
    gives (each user's system prompt, say)::

        assistant = AgentObject("assistant")
        assistant.chat(langgraph.chat(lambda ctx: langgraph.Options(system=system_of(ctx))))
    """
    given: Configure[Options] = options if options is not None else Options()
    if isinstance(given, Options) and not isinstance(given.model, BaseChatModel):
        _provider_of_model(model_of(given.model))

    async def answer(ctx: SessionContext, text: str) -> str:
        return await prompt(ctx, text, await configured(given, ctx))

    return answer


def agent(
    name: str,
    *,
    model: str | BaseChatModel | None = None,
    system: str | None = None,
    tools: Sequence[Tool | BaseTool] = (),
    description: str | None = None,
    schema: Sequence[Step] = (),
    background: bool = False,
    metadata: dict[str, str] | None = None,
    inactivity_timeout: timedelta | None = None,
    abort_timeout: timedelta | None = None,
    ingress_private: bool | None = None,
    invocation_retry_policy: InvocationRetryPolicy | None = RETRIES,
) -> AgentObject:
    """
    A LangGraph agent: an ``AgentObject`` ``name``, one conversation per key,
    whose ``chat`` answers with the usual graph of ``model``, ``system`` and
    ``tools`` (see ``Options``)::

        assistant = langgraph.agent("assistant", system="Answer briefly.", tools=[get_weather])
        sessions.serve(assistant)
    """
    obj = AgentObject(
        name,
        description=description,
        schema=schema,
        background=background,
        metadata=metadata,
        inactivity_timeout=inactivity_timeout,
        abort_timeout=abort_timeout,
        ingress_private=ingress_private,
        invocation_retry_policy=invocation_retry_policy,
    )
    obj.chat(chat(Options(model=model, system=system, tools=tools)))
    return obj
