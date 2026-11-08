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
- An attempt runs at most LangGraph's default ``recursion_limit`` of steps
  (about ten thousand: a model request is one step, its tools another). A
  turn that reaches it ends for good, with a terminal error: a retry would
  carry on from where it stopped, with a fresh limit, and so would never end
  a model that keeps calling tools.
- The model sees the earlier turns as their messages and answers, and the
  latest turns whole (the agent's ``window``), as many as its ``budget``
  holds; the built-in agent has its model summarize the earliest once they
  outgrow it, after a turn's answer (``_conversation.py``).
- The model sees the latest messages of a conversation that lives long: as
  many as fit in ``WINDOW`` of its context window, by LangChain's
  ``trim_messages`` (starting with a prompt, counted by
  ``count_tokens_approximately``). The conversation is stored whole. That
  takes a chat model whose profile has its context window
  (``max_input_tokens``), as LangChain's integrations have for the models
  they know, else the model sees all of it. For a summary of the earlier
  messages instead, pass a graph of your own, such as ``create_agent`` with
  LangChain's ``SummarizationMiddleware``.

For a graph of your own, ``SqlCheckpointSaver`` stores LangGraph's checkpoints
in the object's database instead. Its state is the graph's own: its models
read the state, which the agent's ``window``, ``budget`` and summaries do not
reach, and which grows unless the graph trims it (``_graph.py`` shows how).
"""

from __future__ import annotations

import importlib
import json
import os
import typing
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Any, TypedDict, Unpack

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
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.runtime import get_runtime
from pydantic import BaseModel, PrivateAttr

from restate.agents._common import (
    MODEL_ENV,
    Check,
    ModelError,
    PerSession,
    Summarize,
    check_built_in,
    check_own,
    configured,
    from_tool,
    model_of,
    provider_of,
)
from restate.agents._conversation import Call, Conversation, Item, Parsed, Result, Run, answer_once
from restate.agents._object import AgentObject, AgentOptions
from restate.agents._output import answer_with, asking
from restate.agents._tools import Tool, ToolCall
from restate.agents._turn import settings_of, tool_call_ended, tool_call_started
from restate.agents.langgraph._context import handler_context
from restate.agents.langgraph._errors import terminal_of
from restate.agents.langgraph._graph import AnyGraph, check, run_of
from restate.sessions import SessionContext

M = typing.TypeVar("M", bound=BaseModel)


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

_OPTIONS: dict[str, dict[str, Any]] = {"openai": {"use_responses_api": True}}
"""The options of each provider's chat model. OpenAI's reasoning models take function tools only on the
Responses API (on Chat Completions they refuse them, unless reasoning is off), so its models use it."""


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
    return typing.cast(BaseChatModel, getattr(integration, class_name)(model=model_id, **_OPTIONS.get(provider, {})))


class _RestateTool(BaseTool):
    """A ``restate.agents`` tool as a LangChain tool; it receives its tool call id and validates its own arguments."""

    _tool: Tool = PrivateAttr()
    _ctx: SessionContext | None = PrivateAttr(default=None)

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(f"{self.name} runs asynchronously only")

    async def ainvoke(self, input: Any, config: RunnableConfig | None = None, **kwargs: Any) -> ToolMessage:
        call = typing.cast(dict[str, Any], input)
        call_id = call.get("id") if isinstance(input, dict) else None
        if not call_id:
            raise ValueError(f"{self.name}: a tool call without an id")
        ctx = self._ctx if self._ctx is not None else _run_context(self.name)
        text = await self._tool.run(call.get("args", {}), ToolCall(ctx, str(call_id)))
        return ToolMessage(content=text, tool_call_id=str(call_id), name=self.name)


def _run_context(tool: str) -> SessionContext:
    """Return the handler's context of the graph run in progress (see ``_context.py``)."""
    try:
        run_context = get_runtime().context
    except RuntimeError:
        run_context = None
    ctx = handler_context(run_context)
    if ctx is None:
        raise TypeError(
            f"the tool {tool} runs in a graph that langgraph.agent(graph=...) runs, or that is invoked with the "
            "handler's context: ainvoke(..., context=ctx)"
        )
    return ctx


def _native(tool: Tool, ctx: SessionContext | None) -> BaseTool:
    native = _RestateTool(name=tool.name, description=tool.description, args_schema=tool.input_schema)
    native._tool = tool  # pyright: ignore[reportPrivateUsage]
    native._ctx = ctx  # pyright: ignore[reportPrivateUsage]
    return native


def tools(given: Sequence[AnyTool]) -> list[BaseTool]:
    """Return ``given`` as LangChain tools, for a graph of your own or LangChain's ``create_agent``::

        graph = create_agent(model, tools=langgraph.tools([call(order_status, description="..."), get_weather]))
        desk = langgraph.agent("desk", graph=graph)

    The tools that are Restate's (``call``, ``send``, ``workflow``,
    ``subagent``, ``wait_for_signal``) make their Restate calls once per tool
    call, even if a crash runs the tool call again: the model's reply, with
    the tool call's id, is stored before the tools run. A ``tool`` of your
    own runs your function, again after a crash, as do LangChain tools,
    which are passed through. They take the handler's context from the
    graph's run (``langgraph.agent(graph=...)`` passes it; a handler of your
    own passes ``context=ctx``).

    Args:
        given: ``restate.agents`` tools, and LangChain tools.

    Returns:
        The tools, as LangChain takes them.
    """
    return [_native(t, None) if isinstance(t, Tool) else t for t in given]


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

    def reply(self, text: str, agent: str | None = None) -> Item:
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


WINDOW = 0.75
"""The share of the model's context window that the conversation fills, at most: the rest is for the system prompt,
the tools, the reply, and the approximate count's error."""


def _window(model: BaseChatModel) -> int | None:
    """Return how many tokens of the conversation ``model`` sees: ``WINDOW`` of its context window, if its profile has it.

    LangChain's integrations give their chat models a profile, for the models
    they know; one without it sees all of the conversation.
    """
    limit = model.profile.get("max_input_tokens") if model.profile is not None else None
    return int(limit * WINDOW) if limit is not None and limit > 0 else None


def _latest(messages: Sequence[BaseMessage], window: int | None) -> list[BaseMessage]:
    """Return the latest of ``messages`` that fit in ``window`` tokens, starting with a prompt (LangChain's ``trim_messages``).

    The latest prompt and what followed it are sent whole, even if they do not fit.
    """
    if window is None:
        return list(messages)
    # Typed partly unknown (its text splitter's type).
    trim = typing.cast(Any, langchain_messages.trim_messages)
    fitting: list[BaseMessage] = trim(
        messages,
        max_tokens=window,
        strategy="last",
        token_counter=count_tokens_approximately,
        start_on="human",
    )
    if fitting:
        return fitting
    last_prompt = max((i for i, message in enumerate(messages) if isinstance(message, HumanMessage)), default=0)
    return list(messages[last_prompt:])


def _graph(
    ctx: SessionContext, conversation: Conversation, model: BaseChatModel, tools: list[BaseTool], system: str | None
) -> Any:
    """Build the agent's graph: ``model`` calls ``tools`` until it answers, each step stored in ``conversation``.

    It starts with the tools if the conversation ends with a reply whose tool
    calls have no results (cut off by a crash), and else with the model.
    """
    bound = model.bind_tools(tools) if tools else model
    node = ToolNode(tools)
    window = _window(model)

    async def call_model(state: State) -> dict[str, Any]:
        messages = _latest(state["messages"], window)
        reply = await bound.ainvoke([SystemMessage(system), *messages] if system else messages)
        await conversation.turn.append_reply(reply)
        return {"messages": [reply]}

    async def run_tools(state: State, config: RunnableConfig) -> Any:
        await tool_call_started(ctx)
        try:
            ran = await node.ainvoke(state, config)
        except Exception as error:
            # Mark the error as the tool's, so that it is never mistaken for the model provider refusing.
            from_tool(ctx, error)
            raise
        await conversation.turn.append(*_tool_messages(ran))
        await tool_call_ended(ctx)
        return ran

    def start(state: State) -> str:
        return "tools" if conversation.turn.open_calls() else "model"

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
        if (final := conversation.turn.final_reply()) is not None:
            # The run had stored its final reply, but not its answer: that is the answer, rather than ask again.
            return final
        messages = typing.cast(list[BaseMessage], conversation.history())
        graph = _graph(ctx, conversation, chat_model, tools, system)
        try:
            state = typing.cast(State, await graph.ainvoke({"messages": messages}))
        except Exception as error:
            if (terminal := terminal_of(error)) is not None:
                # LangGraph's own error, which a retry would meet again: its recursion limit, say.
                raise terminal from error
            raise
        return _text(state["messages"][-1])

    return run


@typing.overload
async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    model: ChatModel | None = None,
    system: str | None = None,
    tools: Sequence[AnyTool] = (),
    output: None = None,
) -> str: ...


@typing.overload
async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    model: ChatModel | None = None,
    system: str | None = None,
    tools: Sequence[AnyTool] = (),
    output: type[M],
) -> M: ...


@typing.overload
async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    graph: AnyGraph,
    output: None = None,
    context: Any = None,
) -> str: ...


@typing.overload
async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    graph: AnyGraph,
    output: type[M],
    context: Any = None,
) -> M: ...


async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    graph: AnyGraph | None = None,
    model: ChatModel | None = None,
    system: str | None = None,
    tools: Sequence[AnyTool] = (),
    output: type[BaseModel] | None = None,
    context: Any = None,
) -> Any:
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
        graph: A graph of your own to run, as ``agent(graph=...)`` runs it
            for ``chat``, instead of the built-in tool-calling loop. It takes
            no ``model``, ``system`` or ``tools``.
        model: ``"openai/<model-id>"`` or ``"anthropic/<model-id>"``, or a
            LangChain chat model that calls tools. Defaults to ``$RESTATE_AGENT_MODEL``,
            else ``"openai/gpt-6-sol"``.
        system: The system prompt.
        tools: ``restate.agents`` tools, or LangChain tools.
        output: A Pydantic model, for an answer that is one rather than
            text. The system prompt asks for its JSON; with ``graph``, the
            graph's own prompts must, and its last AI message is validated.
        context: The run context of ``graph`` (LangGraph's
            ``runtime.context``), for a graph with a ``context_schema``.
            Defaults to the handler's context. The prebuilt ``nodes`` and the
            tools of ``tools()`` get the handler's context either way.

    Returns:
        The agent's answer: its text, or a value of ``output``.

    Raises:
        TypeError: If ``graph`` is given with ``model``, ``system`` or
            ``tools``, its state has no ``messages``, or it is compiled
            with static breakpoints (``interrupt_before``,
            ``interrupt_after``); or if ``context`` is given without
            ``graph``.
        TerminalError: If the agent's reply is not a value of ``output``,
            twice.
    """
    if graph is not None:
        check_own(graph, "graph", model=model, system=system, tools=tools)
        check(graph)
        run = run_of(ctx, graph, context)
        # The graph's run reads the previous turn whole, to give the graph what closed it (see ``_closing``).
        window = settings_of(ctx).window
        whole = window if window == "all" else max(window, 1)

        def ask(text: str, check: Check | None = None) -> Awaitable[str]:
            return answer_once(ctx, text, ITEMS, run, whole, check=check)

    else:
        check_built_in(graph, "graph", context=context)
        # Without a way to ask for ``output`` natively with any provider's model, the system prompt asks for it.
        asked = system if output is None else asking(system, output)

        def ask(text: str, check: Check | None = None) -> Awaitable[str]:
            return answer_once(ctx, text, ITEMS, _run(ctx, model, asked, tools), check=check)

    if output is None:
        return await ask(text)
    return await answer_with(output, text, ask)


@typing.overload
def agent(
    name: str,
    *,
    model: PerSession[ChatModel | None] = None,
    system: PerSession[str | None] = None,
    tools: PerSession[Sequence[AnyTool]] = (),
    output: type[BaseModel] | None = None,
    **options: Unpack[AgentOptions],
) -> AgentObject: ...


@typing.overload
def agent(
    name: str,
    *,
    graph: AnyGraph,
    output: type[BaseModel] | None = None,
    context: PerSession[Any] = None,
    **options: Unpack[AgentOptions],
) -> AgentObject: ...


def agent(
    name: str,
    *,
    graph: AnyGraph | None = None,
    model: PerSession[ChatModel | None] = None,
    system: PerSession[str | None] = None,
    tools: PerSession[Sequence[AnyTool]] = (),
    output: type[BaseModel] | None = None,
    context: PerSession[Any] = None,
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

    Or bring a graph of your own, built on LangGraph's ``MessagesState``:
    ``chat`` then runs it, with the message as a new ``HumanMessage``, and
    answers with its last AI message. Its checkpoints are in the key's
    database, and the prebuilt ``nodes`` (a signal, a workflow, a call,
    another agent) and the tools of ``tools()`` get the handler's context in
    it::

        desk = langgraph.agent("desk", graph=builder)

    A graph with a ``context_schema`` of its own gets its run context from
    ``context``, a value or a function of the handler's context::

        desk = langgraph.agent("desk", graph=graph, context=lambda ctx: Account(ctx.key))

    Args:
        name: The object's name, as Restate serves it.
        graph: A graph of your own, instead of the built-in tool-calling
            loop: a ``StateGraph`` whose state has ``messages``, or a
            compiled one (such as LangChain's ``create_agent`` makes). It has
            its own model and tools, so it takes no ``model``, ``system`` or
            ``tools``.
        model: ``"openai/<model-id>"`` (needs ``langchain-openai``) or
            ``"anthropic/<model-id>"`` (needs ``langchain-anthropic``), or a
            LangChain chat model that calls tools. Defaults to ``$RESTATE_AGENT_MODEL``,
            else ``"openai/gpt-6-sol"``.
        system: The system prompt.
        tools: ``restate.agents`` tools, or LangChain tools.
        output: A Pydantic model, for answers that are one rather than
            text. The system prompt asks for its JSON, which ``chat``
            returns; with ``graph``, the graph's own prompts must, and its
            last AI message is validated.
        context: The run context of ``graph`` (LangGraph's
            ``runtime.context``), for a graph with a ``context_schema``: a
            value, or a function of the invocation's context. Defaults to
            the handler's context.
        **options: The object's options, such as ``description``,
            ``background`` or ``invocation_retry_policy``: see ``AgentOptions``.

    Returns:
        The agent, to serve with ``restate.serve(restate.app([agent]))``, or any ASGI server.

    Raises:
        TypeError: If ``graph`` is given with ``model``, ``system`` or
            ``tools``, its state has no ``messages``, or it is compiled
            with static breakpoints (``interrupt_before``,
            ``interrupt_after``); or if ``context`` is given without
            ``graph``.
        TerminalError: If ``model`` names a provider other than OpenAI or Anthropic.
    """
    if graph is not None:
        check_own(graph, "graph", model=model, system=system, tools=tools)
        check(graph)
        return _graph_agent(name, graph, output, context, **options)
    check_built_in(graph, "graph", context=context)
    if model is None or isinstance(model, str):
        _provider_of_model(model_of(model))
    obj = AgentObject(name, **options)

    async def answer(ctx: SessionContext, text: str) -> Any:
        return await prompt(
            ctx,
            text,
            model=await configured(model, ctx),
            system=await configured(system, ctx),
            tools=await configured(tools, ctx),
            output=output,
        )

    obj.chat(answer, items=ITEMS, output=output, summarize=_summarizer(model))
    return obj


def _summarizer(model: PerSession[ChatModel | None]) -> Summarize:
    """Return how the built-in agent summarizes its earlier turns: with its own model, in a request of its own."""

    async def summarize(ctx: SessionContext, request: str) -> str:
        chosen = await configured(model, ctx)
        chat_model = chosen if isinstance(chosen, BaseChatModel) else _chat_model(model_of(chosen))
        return _text(await chat_model.ainvoke([HumanMessage(request)]))

    return summarize


def _graph_agent(
    name: str,
    graph: AnyGraph,
    output: type[BaseModel] | None,
    context: PerSession[Any],
    **options: Unpack[AgentOptions],
) -> AgentObject:
    """Create an agent whose ``chat`` runs ``graph`` (see ``_graph.py``)."""
    obj = AgentObject(name, **options)

    async def answer(ctx: SessionContext, text: str) -> Any:
        return await prompt(ctx, text, graph=graph, output=output, context=await configured(context, ctx))

    obj.chat(answer, items=ITEMS, output=output)
    return obj
