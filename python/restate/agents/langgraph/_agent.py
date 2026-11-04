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
"""LangGraph agents whose conversations are stored in the object's database, one step at a time.

The graph is the usual tool-calling loop: a model node and a tools node, going
back to the model until it answers. It runs on the shared conversation of
``restate.agents`` (``_conversation.py``), which answers each prompt once and
closes a stopped, refused or failed run. The handler is not replayed: a
retried attempt (after a crash or a lost connection) runs again from the
start, and the run resumes from the last step stored:

- The model node stores each reply before the tools run, and the tools node
  stores their results before the next request. A crash while tools are
  running runs them again with the same tool call ids, which a tool can pass
  on to stay idempotent.
- A tool's error fails the attempt, and Restate retries it. (LangGraph's
  ``ToolNode`` would otherwise hand the error to the model as the tool's
  result.) A call to a tool the agent does not have, or with arguments that do
  not validate, is reported back to the model to fix.

For a graph of your own, ``SqlCheckpointSaver`` stores LangGraph's checkpoints
in the object's database instead.
"""

from __future__ import annotations

import importlib
import json
import os
import typing
from collections.abc import Callable, Sequence
from typing import Annotated, Any, TypedDict

from langchain_core import messages as langchain_messages
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import PrivateAttr

from restate.agents._common import MODEL_ENV, ModelError, PerSession, configured, from_tool, model_of, provider_of
from restate.agents._conversation import Call, Conversation, Item, Parsed, Result, Run, answer_once
from restate.agents._object import AgentObject, AgentOptions
from restate.agents._tools import Tool, ToolCall
from restate.agents._turn import tool_call_ended, tool_call_started
from restate.sessions import SessionContext

if typing.TYPE_CHECKING:
    from typing_extensions import Unpack


class State(TypedDict):
    """The state of the agent's graph: the conversation."""

    messages: Annotated[list[AnyMessage], add_messages]


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


# LangChain's own (de)serialization of messages, whose dicts it leaves untyped.
_to_dict = typing.cast(Callable[[BaseMessage], Item], langchain_messages.message_to_dict)
_from_dicts = typing.cast(Callable[[list[Item]], list[BaseMessage]], langchain_messages.messages_from_dict)


def _text(message: BaseMessage) -> str:
    return str(message.text)


class _Items:
    """LangChain's messages, as the conversation keeps them: stored as LangChain's dicts, in JSON."""

    def dump(self, item: Item) -> str:
        return json.dumps(_to_dict(item))

    def load(self, stored: str) -> Item:
        [message] = _from_dicts([json.loads(stored)])
        return message

    def prompt(self, text: str) -> Item:
        return HumanMessage(text)

    def reply(self, text: str) -> Item:
        return AIMessage(text)

    def results(self, results: Sequence[tuple[Call, str]]) -> list[Item]:
        return [ToolMessage(text, tool_call_id=call.id, name=call.name, status="error") for call, text in results]

    def read(self, item: Item) -> Parsed:
        message = typing.cast(BaseMessage, item)
        if isinstance(message, HumanMessage):
            return Parsed(("user", _text(message)))
        if isinstance(message, AIMessage):
            calls = [Call(call.get("id") or "", call["name"], call["args"]) for call in message.tool_calls]
            return Parsed(("assistant", _text(message)), calls)
        if isinstance(message, ToolMessage):
            return Parsed(results=[Result(message.tool_call_id, _text(message), message.status == "error")])
        return Parsed()


ITEMS = _Items()


def _tool_messages(ran: Any) -> list[BaseMessage]:
    """Return the tool messages a ``ToolNode`` returned: as its state update, or as a list."""
    messages = typing.cast(dict[str, Any], ran)["messages"] if isinstance(ran, dict) else ran
    return typing.cast(list[BaseMessage], messages)


def _graph(
    ctx: SessionContext, conversation: Conversation, model: BaseChatModel, tools: list[BaseTool], system: str | None
) -> Any:
    """Build the agent's graph: ``model`` calls ``tools`` until it answers, each step stored in ``conversation``.

    It starts with the tools if the conversation ends with a reply whose tool
    calls have no results (cut off by a crash), and else with the model.
    """
    bound = model.bind_tools(tools) if tools else model
    node = ToolNode(tools)

    async def call_model(state: State) -> dict[str, Any]:
        messages = state["messages"]
        reply = await bound.ainvoke([SystemMessage(system), *messages] if system else messages)
        await conversation.append_reply(reply)
        return {"messages": [reply]}

    async def run_tools(state: State, config: RunnableConfig) -> Any:
        await tool_call_started(ctx)
        try:
            ran = await node.ainvoke(state, config)
        except Exception as error:
            # Mark the error as the tool's, so that it is never mistaken for the model provider refusing.
            from_tool(ctx, error)
            raise
        await conversation.append(*_tool_messages(ran))
        await tool_call_ended(ctx)
        return ran

    def start(state: State) -> str:
        return "tools" if conversation.unanswered() else "model"

    graph = StateGraph(State)
    graph.add_node("model", call_model)
    graph.add_node("tools", typing.cast(Any, run_tools))
    graph.add_conditional_edges(START, start, {"tools": "tools", "model": "model"})
    graph.add_conditional_edges("model", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "model")
    return graph.compile()


def _run(ctx: SessionContext, model: ChatModel | None, system: str | None, given: Sequence[AnyTool]) -> Run:
    """Return the run of a prompt: the graph, from the conversation to the prompt's answer."""
    chat_model = model if isinstance(model, BaseChatModel) else _chat_model(model_of(model))
    tools = [_native(t, ctx) if isinstance(t, Tool) else t for t in given]

    async def run(conversation: Conversation) -> str:
        if (final := conversation.final_reply()) is not None:
            # The run had stored its final reply, but not its answer: that is the answer, rather than ask again.
            return final
        messages = typing.cast(list[BaseMessage], [entry.item for entry in conversation.entries])
        graph = _graph(ctx, conversation, chat_model, tools, system)
        state = typing.cast(State, await graph.ainvoke({"messages": messages}))
        return _text(state["messages"][-1])

    return run


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

    return await answer_once(ctx, text, ITEMS, _run(ctx, model, system, tools))


def agent(
    name: str,
    *,
    model: PerSession[ChatModel | None] = None,
    system: PerSession[str | None] = None,
    tools: PerSession[Sequence[AnyTool]] = (),
    **options: Unpack[AgentOptions],
) -> AgentObject:
    """Create a LangGraph agent: a virtual object with one conversation per key.

    Its ``chat`` handler answers each message with LangGraph's tool-calling
    loop; the conversation is kept in the key's database, a step at a time.
    Add handlers of your own with ``@assistant.handler``::

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
        **options: The object's options, such as ``description``,
            ``background`` or ``invocation_retry_policy``: see ``AgentOptions``.

    Returns:
        The agent, to serve with ``restate.serve(sessions.app(agent))``, or any ASGI server.

    Raises:
        TerminalError: If ``model`` names a provider other than OpenAI or Anthropic.
    """
    if model is None or isinstance(model, str):
        _provider_of_model(model_of(model))
    obj = AgentObject(name, **options)

    async def answer(ctx: SessionContext, text: str) -> str:
        return await prompt(
            ctx,
            text,
            model=await configured(model, ctx),
            system=await configured(system, ctx),
            tools=await configured(tools, ctx),
        )

    obj.chat(answer, items=ITEMS)
    return obj
