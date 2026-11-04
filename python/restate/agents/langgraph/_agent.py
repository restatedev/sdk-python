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
"""LangGraph agents whose checkpoints are stored in the object's database.

The graph is the usual tool-calling loop: a model node and a tools node, going
back to the model until it answers. Checkpoints are stored by
``SqlCheckpointSaver`` (``_checkpointer.py``). The handler is not replayed: a
retried attempt (after a crash or a lost connection) runs again from the
start, and LangGraph resumes from the last step it stored:

- Each step is stored before the next one starts (``durability="sync"``), in
  order with everything else the handler does. The tool calls a model made are
  therefore stored before any tool runs, and a crash while tools are running
  resumes them with the same tool call ids, which a tool can pass on to stay
  idempotent.
- A tool's error fails the attempt, and Restate retries it. (LangGraph's
  ``ToolNode`` would otherwise hand the error to the model as the tool's
  result.) A call to a tool the agent does not have, or with arguments that do
  not validate, is reported back to the model to fix.
- Each message is answered once per invocation: the graph records each answer
  in its state under the prompt's id, and a retried attempt returns it.
"""

from __future__ import annotations

import importlib
import os
import typing
from collections.abc import Awaitable, Callable, Sequence
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

from restate.agents._calls import outcomes
from restate.agents._common import (
    CANCELLED,
    MODEL_ENV,
    UNANSWERED,
    ModelError,
    PerSession,
    configured,
    cut_off_result,
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
from restate.sessions import Migration, SessionContext

_CANCELLATION = 409
"""The status code of Restate's cancellation error, which a stopped turn fails with."""


def _merged(old: dict[str, str], new: dict[str, str]) -> dict[str, str]:
    return {**old, **new}


class State(TypedDict, total=False):
    """The state of the agent's graph: the conversation, the prompt being answered, and each prompt's answer."""

    messages: Annotated[list[AnyMessage], add_messages]
    prompt: str
    """The id of the prompt being answered."""
    answers: Annotated[dict[str, str], _merged]
    """The answer to each prompt, by id."""


ChatModel: typing.TypeAlias = str | BaseChatModel
"""A model: ``"openai/<model-id>"`` or ``"anthropic/<model-id>"``, or a LangChain chat model that calls tools."""

AnyTool: typing.TypeAlias = Tool | BaseTool
"""A tool: one made with ``restate.agents`` (``tool``, ``call``, ``wait_for_signal``, ...) or a LangChain tool."""


_PROVIDERS = {
    "openai": ("langchain_openai", "ChatOpenAI", "langchain-openai"),
    "anthropic": ("langchain_anthropic", "ChatAnthropic", "langchain-anthropic"),
}
"""The providers that can be named in a model string, each with its LangChain module, class and package."""


def _provider_of_model(named: str) -> tuple[str, str]:
    """Split ``provider/model-id`` into provider and model id, or raise ``ModelError`` for an unsupported provider."""
    provider, model_id = provider_of(named)
    if provider not in _PROVIDERS or not model_id:
        from_env = f" (from {MODEL_ENV})" if os.environ.get(MODEL_ENV) == named else ""
        raise ModelError(f'the model "{named}"{from_env} is neither openai/<model-id> nor anthropic/<model-id>')
    return provider, model_id


def _chat_model(named: str) -> BaseChatModel:
    """Create the LangChain chat model that ``provider/model-id`` names.

    The provider's LangChain package is imported only when one of its models is
    used, so only that package needs to be installed next to the ``langgraph``
    extra.
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
    """A ``restate.agents`` tool as a LangChain tool; it receives its tool call id and validates its own arguments."""

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
    """Return the graph's tools node, which runs the tool calls of the model's last message.

    A ``ToolNode`` runs them, and tells the model what went wrong when a call
    names an unknown tool or has invalid arguments; any other tool error fails
    the attempt. The start and end of each round are published for clients that
    follow the conversation.
    """
    node = ToolNode(tools)

    async def run_tools(state: State, config: RunnableConfig) -> Any:
        await tool_call_started(ctx)
        try:
            ran = await node.ainvoke(state, config)
        except Exception as error:
            # Mark the error as the tool's, so that it is never mistaken for the model provider refusing.
            from_tool(ctx, error)
            raise
        await tool_call_ended(ctx)
        return ran

    return run_tools


def _text(message: BaseMessage) -> str:
    return str(message.text)


def _graph(ctx: SessionContext, model: BaseChatModel, tools: list[BaseTool], system: str | None) -> Any:
    """Build the agent's graph: ``model`` calls ``tools`` until it answers the prompt."""
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


async def _answer_once(
    ctx: SessionContext, text: str, model: ChatModel | None, system: str | None, given: Sequence[AnyTool]
) -> str:
    chat_model = model if isinstance(model, BaseChatModel) else _chat_model(model_of(model))
    tools = [_native(t, ctx) if isinstance(t, Tool) else t for t in given]
    # An agent resumes only from the latest checkpoint; keeping earlier ones would store the conversation again
    # at each step.
    app: Compiled = _graph(ctx, chat_model, tools, system).compile(
        checkpointer=SqlCheckpointSaver(ctx.db, history="latest")
    )
    config: RunnableConfig = {"configurable": {"thread_id": ctx.key}}
    prompt_id = next_prompt_id(ctx)
    while True:
        values = typing.cast(State, (await app.aget_state(config)).values)
        answers = values.get("answers", {})
        if prompt_id in answers:
            return stored_answer(answers[prompt_id])
        # Finish an interrupted run first (this prompt's own, after a crash), then start this prompt's run.
        # A run is unfinished while its prompt has no answer.
        last = values.get("prompt")
        interrupted = last is not None and last not in answers
        start: State | None = None if interrupted else {"messages": [HumanMessage(text)], "prompt": prompt_id}
        stop = stop_signal_of(ctx)
        run = app.ainvoke(start, config, durability="sync")
        try:
            await (stop.guard(run) if stop is not None else run)
        except AttemptEnded:
            # The attempt ended: stop here, and let the next attempt continue the run.
            raise
        except TerminalError as error:
            # Cancelled: close the run, so that the next prompt starts afresh.
            if error.status_code == _CANCELLATION:
                await _close(ctx, app, config, CANCELLED)
                raise
            # Failed for good (a LangChain tool's terminal error): close the run with the error, so that the next
            # prompt does not run into it again. If the run was an earlier prompt's, carry on with this one.
            await _close(ctx, app, config, f"Error: {error.message}")
            if not interrupted or last == prompt_id:
                raise
        except Exception as error:
            # Refused for good (a wrong API key, say): close the run unanswered and fail with a terminal error.
            # If the run was an earlier prompt's, carry on with this one.
            refused = refusal_of(ctx, error)
            if refused is None:
                raise
            await _close(ctx, app, config, UNANSWERED)
            if not interrupted or last == prompt_id:
                raise refused from error


async def _close(ctx: SessionContext, app: Compiled, config: RunnableConfig, text: str) -> None:
    """Close a cancelled, refused or failed run of ``app`` with ``text``, as its ``answer`` node would.

    Each tool call of the last reply that has no result gets ``text`` as its
    result, and ``text`` becomes the reply and the prompt's answer. A run that
    stored no input yet (cancelled right away) has nothing to close: the state's
    prompt is an earlier one, which already has its answer.
    """
    values = typing.cast(State, (await app.aget_state(config)).values)
    prompt = values.get("prompt")
    if prompt is None or prompt in values.get("answers", {}):
        return
    messages = values.get("messages", [])
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    last = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    calls = [c for c in (last.tool_calls if last else []) if c.get("id") and c.get("id") not in answered]
    returned = await outcomes(ctx, [str(c.get("id")) for c in calls])
    closing: list[BaseMessage] = [
        *(
            ToolMessage(cut_off_result(returned, str(c.get("id")), text), tool_call_id=str(c.get("id")), status="error")
            for c in calls
        ),
        AIMessage(text),
    ]
    await app.aupdate_state(config, {"messages": closing, "answers": {prompt: text}}, as_node="answer")


async def _transcript(ctx: SessionContext) -> Transcript:
    """Return the thread (the object's key) from its last checkpoint, in the form ``messages`` returns."""
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


async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    model: ChatModel | None = None,
    system: str | None = None,
    tools: Sequence[AnyTool] = (),
) -> str:
    """Answer a prompt with a LangGraph agent, exactly once.

    For a handler of your own; the conversation is the object's key, shared
    with its ``chat``. A retried attempt resumes the same prompt rather than
    asking again, and each prompt of an invocation is answered on its own::

        @assistant.handler
        async def summarize(ctx: sessions.SessionContext, topic: str) -> str:
            return await langgraph.prompt(ctx, f"Summarize what we said about {topic}")

    Args:
        ctx: The handler's context.
        text: The prompt.
        model: ``"openai/<model-id>"`` or ``"anthropic/<model-id>"``, or a
            LangChain chat model that calls tools. Defaults to ``$RESTATE_AGENT_MODEL``,
            else ``"openai/gpt-6-sol"``.
        system: The system prompt.
        tools: ``restate.agents`` tools, or LangChain tools.

    Returns:
        The agent's answer.
    """

    async def read() -> Transcript:
        return await _transcript(ctx)

    async def work() -> str:
        return await _answer_once(ctx, text, model, system, tools)

    return await transcribing(ctx, read, work)


def agent(
    name: str,
    *,
    model: PerSession[ChatModel | None] = None,
    system: PerSession[str | None] = None,
    tools: PerSession[Sequence[AnyTool]] = (),
    description: str | None = None,
    migrations: Sequence[Migration] = (),
    background: bool = False,
    metadata: dict[str, str] | None = None,
    inactivity_timeout: timedelta | None = None,
    abort_timeout: timedelta | None = None,
    ingress_private: bool | None = None,
    invocation_retry_policy: InvocationRetryPolicy | None = RETRIES,
) -> AgentObject:
    """Create a LangGraph agent: a virtual object with one conversation per key.

    Its ``chat`` handler answers each message with LangGraph's tool-calling
    loop; LangGraph's checkpoints are kept in the key's database. Add
    handlers of your own with ``@assistant.handler``::

        assistant = langgraph.agent("assistant", system="Answer briefly.", tools=[get_weather])

    ``model``, ``system`` and ``tools`` may each be a function of the
    invocation's context, sync or async, for values that differ per user::

        assistant = langgraph.agent("assistant", system=lambda ctx: f"You help {ctx.key}.")

    Args:
        name: The object's name, as Restate serves it.
        model: ``"openai/<model-id>"`` (needs ``langchain-openai``) or
            ``"anthropic/<model-id>"`` (needs ``langchain-anthropic``), or a
            LangChain chat model that calls tools. Defaults to ``$RESTATE_AGENT_MODEL``,
            else ``"openai/gpt-6-sol"``.
        system: The system prompt.
        tools: ``restate.agents`` tools, or LangChain tools.
        description: The object's description, shown in Restate's UI.
        migrations: Tables of your own, beside the agent's.
        background: Whether it works unwatched (a subagent, say): it then
            publishes its progress once a turn ends, not at each step.
        metadata: The object's metadata.
        inactivity_timeout: Restate's inactivity timeout for its handlers.
        abort_timeout: Restate's abort timeout for its handlers.
        ingress_private: Whether only other services may call it.
        invocation_retry_policy: How Restate retries a failed turn.

    Returns:
        The agent, to serve with ``restate.serve(sessions.app(agent))``, or any ASGI server.

    Raises:
        TerminalError: If ``model`` names a provider other than OpenAI or Anthropic.
    """
    if model is None or isinstance(model, str):
        _provider_of_model(model_of(model))
    obj = AgentObject(
        name,
        description=description,
        migrations=migrations,
        background=background,
        metadata=metadata,
        inactivity_timeout=inactivity_timeout,
        abort_timeout=abort_timeout,
        ingress_private=ingress_private,
        invocation_retry_policy=invocation_retry_policy,
    )

    async def answer(ctx: SessionContext, text: str) -> str:
        return await prompt(
            ctx,
            text,
            model=await configured(model, ctx),
            system=await configured(system, ctx),
            tools=await configured(tools, ctx),
        )

    obj.chat(answer)
    return obj
