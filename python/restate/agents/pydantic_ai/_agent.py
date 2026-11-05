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
"""Pydantic AI agents whose conversations are stored in the object's database, one step at a time.

The handler is not replayed: a retried attempt (after a crash or a lost
connection) runs again from the start, and the run resumes from the last step
stored:

- The conversation is stored before each model request and with each model
  response, in order with everything else the handler does. The tool calls a
  model made are therefore stored before any tool runs, and a crash while tools
  are running runs them again with the same tool call ids, which a tool can
  pass on to stay idempotent.
- A tool's error fails the attempt, and Restate retries it. A
  ``restate.TerminalError``, which a retry would only repeat, is instead
  returned to the model as the tool's answer, and so are arguments that do not
  validate (Pydantic AI asks the model again).
- Each message is answered once per invocation, by the shared conversation of
  ``restate.agents`` (``_conversation.py``), which also closes a stopped,
  refused or failed run.
"""

from __future__ import annotations

import typing
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Unpack

from pydantic import BaseModel, ConfigDict, TypeAdapter
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestContext, infer_model
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset

from pydantic_ai import Agent, RunContext
from pydantic_ai import Tool as PydanticTool
from restate.agents._common import (
    CANCELLATION,
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
from restate.agents._output import with_output
from restate.agents._tools import Tool, ToolCall
from restate.agents._turn import stop_signal_of, tool_call_ended, tool_call_started
from restate.exceptions import TerminalError
from restate.sessions import SessionContext

M = typing.TypeVar("M", bound=BaseModel)

AnyModel: typing.TypeAlias = str | Model
"""A model: ``"<provider>/<model-id>"`` (or Pydantic AI's ``"<provider>:<model-id>"``), or a Pydantic AI model."""

AnyTool: typing.TypeAlias = Tool | PydanticTool[Any] | Callable[..., Any]
"""A tool: one made with ``restate.agents`` (``tool``, ``call``, ...), a Pydantic AI tool, or a plain function."""


def _toolset(given: Sequence[AnyTool], ctx: SessionContext) -> FunctionToolset[Any]:
    """Return ``given`` as a Pydantic AI toolset for a run in ``ctx``.

    ``restate.agents`` tools are wrapped so that they receive their tool call id
    (they validate their own arguments); Pydantic AI tools and tool functions are
    passed through unchanged.
    """

    def native(tool: Tool) -> PydanticTool[Any]:
        async def run(run_ctx: RunContext[Any], **args: Any) -> str:
            return await tool.run(args, ToolCall(ctx, run_ctx.tool_call_id or ""))

        return PydanticTool.from_schema(run, tool.name, tool.description, tool.input_schema, takes_ctx=True)

    return FunctionToolset([native(t) if isinstance(t, Tool) else t for t in given])


# ModelMessage is an Annotated union (discriminated), which TypeAdapter takes though its type says otherwise.
# Bytes (a tool's binary result, say) as base64, as pydantic-ai's own ModelMessagesTypeAdapter stores them.
_MESSAGE: TypeAdapter[ModelMessage] = TypeAdapter(
    typing.cast(Any, ModelMessage), config=ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")
)


def _result(part: ToolReturnPart | RetryPromptPart) -> str:
    """Return a tool call's result as the model sees it: the return value, or the retry prompt."""
    return part.model_response() if isinstance(part, RetryPromptPart) else part.model_response_str()


def _text(response: ModelMessage) -> str:
    return "".join(part.content for part in response.parts if isinstance(part, TextPart))


class _Items:
    """Pydantic AI's messages, as the conversation keeps them: stored as JSON, loaded straight back by pydantic."""

    def dump(self, item: Item) -> str:
        return _MESSAGE.dump_json(item).decode()

    def load(self, stored: str) -> Item:
        return _MESSAGE.validate_json(stored)

    def prompt(self, text: str) -> Item:
        return ModelRequest(parts=[UserPromptPart(text)])

    def reply(self, text: str) -> Item:
        return ModelResponse(parts=[TextPart(text)])

    def results(self, results: Sequence[tuple[Call, str]]) -> list[Item]:
        if not results:
            return []
        returns = [ToolReturnPart(call.name, text, call.id) for call, text in results]
        return [ModelRequest(parts=list(returns))]

    def read(self, item: Item) -> Parsed:
        message = typing.cast(ModelMessage, item)
        if isinstance(message, ModelResponse):
            said = ("assistant", _text(message)) if any(isinstance(p, TextPart) for p in message.parts) else None
            calls = [Call(call.tool_call_id, call.tool_name, call.args_as_dict()) for call in message.tool_calls]
            return Parsed(said, calls)
        prompts = [p.content for p in message.parts if isinstance(p, UserPromptPart)]
        results = [
            Result(part.tool_call_id, _result(part), isinstance(part, RetryPromptPart))
            for part in message.parts
            if isinstance(part, ToolReturnPart | RetryPromptPart) and part.tool_call_id
        ]
        said = ("user", "".join(p if isinstance(p, str) else "" for p in prompts)) if prompts else None
        return Parsed(said, results=results)


ITEMS = _Items()


def _result_ids(message: ModelMessage) -> set[str]:
    """Return the ids of the tool calls whose results ``message`` holds."""
    if not isinstance(message, ModelRequest):
        return set()
    return {p.tool_call_id for p in message.parts if isinstance(p, ToolReturnPart | RetryPromptPart) and p.tool_call_id}


class _Restate(AbstractCapability[Any]):
    """Make a Pydantic AI run durable in a Restate handler.

    It stores the requests before each model request and each response as it
    arrives, and publishes each tool call as it starts and ends for clients
    that follow the conversation. A tool's ``restate.TerminalError`` becomes its
    answer to the model; any other error fails the attempt.
    """

    def __init__(self, ctx: SessionContext, conversation: Conversation) -> None:
        self.ctx = ctx
        self.conversation = conversation

    async def store(self, messages: Sequence[ModelMessage]) -> None:
        """Store the tools' results in ``messages`` (the run's) that the conversation does not have yet.

        The rest of what a run adds, its responses, is stored as each arrives.
        Results are found by their call ids, not by their place: a history
        processor (``ProcessHistory``) may have trimmed the run's messages, and
        what it makes of them is the model's to see, not the conversation's.
        """
        new = [m for m in messages if any(not self.conversation.has_result(i) for i in _result_ids(m))]
        if new:
            await self.conversation.append(*new)

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        await self.store(ctx.messages)
        return request_context

    async def after_model_request(
        self, ctx: RunContext[Any], *, request_context: ModelRequestContext, response: ModelResponse
    ) -> ModelResponse:
        await self.store(ctx.messages)
        await self.conversation.append_reply(response)
        return response

    async def before_tool_execute(
        self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition, args: Any
    ) -> Any:
        await tool_call_started(self.ctx)
        return args

    async def after_tool_execute(
        self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition, args: Any, result: Any
    ) -> Any:
        await tool_call_ended(self.ctx)
        return result

    async def on_tool_execute_error(
        self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition, args: Any, error: Exception
    ) -> Any:
        stop = stop_signal_of(self.ctx)
        if stop is not None and stop.reason is not None:
            raise stop.reason from error
        if isinstance(error, TerminalError) and error.status_code != CANCELLATION:
            await tool_call_ended(self.ctx)
            return f"Error: {error.message}"
        # Mark the error as the tool's, so that it is never mistaken for the model provider refusing.
        from_tool(self.ctx, error)
        raise error


def _model(model: str | Model | None) -> Model:
    """Resolve ``provider/model-id`` (or ``provider:model-id``) to a Pydantic AI model.

    Raise ``ModelError`` if the provider's package is missing or its API key is
    not set, since every message would then fail the same way.
    """
    if isinstance(model, Model):
        return model
    named = model_of(model)
    try:
        provider, model_id = provider_of(named)
        return infer_model(f"{provider}:{model_id}" if provider else named)
    except (ImportError, UserError, ValueError) as error:
        raise ModelError(f'the model "{named}": {error}') from error


def _run(
    ctx: SessionContext,
    own: Agent[Any, str] | None,
    named: AnyModel | None,
    system: str | None,
    given: Sequence[AnyTool],
) -> Run:
    """Return the run of a prompt: Pydantic AI's, from the conversation to the prompt's answer."""
    # Resolve the model of an Agent of your own here as well, so that a missing key or package fails the same way.
    model = _model(named if named is not None else own.model if own is not None else None)
    agent: Agent[Any, str] = own or Agent(instructions=system)
    toolsets = [_toolset(given, ctx)] if given else []

    async def run(conversation: Conversation) -> str:
        if (final := conversation.final_reply()) is not None:
            # The run had stored its final reply, but not its answer: that is the answer, rather than ask again.
            return final
        history = typing.cast(list[ModelMessage], [entry.item for entry in conversation.entries])
        restate = _Restate(ctx, conversation)
        result = await agent.run(message_history=history, model=model, toolsets=toolsets, capabilities=[restate])
        # The messages that no model request stored: an output tool's return, say.
        await restate.store(result.all_messages())
        return result.output

    return run


@typing.overload
async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    agent: Agent[Any, str] | None = None,
    model: AnyModel | None = None,
    system: str | None = None,
    tools: Sequence[AnyTool] = (),
    output: None = None,
) -> str: ...


@typing.overload
async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    agent: Agent[Any, str] | None = None,
    model: AnyModel | None = None,
    system: str | None = None,
    tools: Sequence[AnyTool] = (),
    output: type[M],
) -> M: ...


async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    agent: Agent[Any, str] | None = None,
    model: AnyModel | None = None,
    system: str | None = None,
    tools: Sequence[AnyTool] = (),
    output: type[BaseModel] | None = None,
) -> Any:
    """Answer a prompt with a Pydantic AI agent, exactly once.

    For a handler of your own; the conversation is the object's key, shared
    with its ``chat``. A retried attempt resumes the same prompt rather than
    asking again, and each prompt of an invocation is answered on its own::

        @assistant.handler
        async def summarize(ctx: sessions.SessionContext, topic: str) -> str:
            return await pydantic_ai.prompt(ctx, f"Summarize what we said about {topic}")

    Args:
        ctx: The handler's context.
        text: The prompt.
        agent: A Pydantic AI ``Agent`` of your own, whose output is text.
            Defaults to one with ``system`` as its instructions.
        model: ``"<provider>/<model-id>"`` of any of Pydantic AI's providers,
            or a Pydantic AI model. Defaults to ``agent``'s own, else
            ``$RESTATE_AGENT_MODEL``, else ``"openai/gpt-6-sol"``.
        system: The instructions, when there is no ``agent``.
        tools: ``restate.agents`` tools, Pydantic AI tools or tool functions,
            beside ``agent``'s own.
        output: A Pydantic model, for an answer that is one rather than
            text. The agent then answers with its JSON.

    Returns:
        The agent's answer: its text, or a value of ``output``.

    Raises:
        TypeError: If both ``agent`` and ``system`` are given.
        TerminalError: If the agent's reply is not a value of ``output``,
            twice.
    """
    check_own(agent, system)

    def ask(text: str, system: str | None) -> Awaitable[str]:
        return answer_once(ctx, text, ITEMS, _run(ctx, agent, model, system, tools))

    if output is None:
        return await ask(text, system)
    return await with_output(output, text, system, ask, own=agent is not None)


def agent(
    name: str,
    pydantic_agent: Agent[Any, str] | None = None,
    /,
    *,
    model: PerSession[AnyModel | None] = None,
    system: PerSession[str | None] = None,
    tools: PerSession[Sequence[AnyTool]] = (),
    output: type[BaseModel] | None = None,
    **options: Unpack[AgentOptions],
) -> AgentObject:
    """Create a Pydantic AI agent: a virtual object with one conversation per key.

    Its ``chat`` handler answers each message with Pydantic AI; the
    conversation is kept in the key's database, a step at a time. Add
    handlers of your own with ``@assistant.handler``::

        assistant = pydantic_ai.agent("assistant", system="Answer briefly.", tools=[get_weather])

    Or bring an ``Agent`` of your own, with this package's tools beside its own::

        assistant = pydantic_ai.agent("assistant", Agent("anthropic:claude-sonnet-5-5"), tools=[refund])

    ``model``, ``system`` and ``tools`` may each be a function of the
    invocation's context, sync or async, for values that differ per user::

        assistant = pydantic_ai.agent("assistant", system=lambda ctx: f"You help {ctx.key}.")

    Args:
        name: The object's name, as Restate serves it.
        pydantic_agent: A Pydantic AI ``Agent`` of your own, whose output is
            text. Defaults to one with ``system`` as its instructions.
        model: ``"<provider>/<model-id>"`` of any of Pydantic AI's providers
            (each needs its extra, such as ``pydantic-ai-slim[openai]``), or a
            Pydantic AI model. Defaults to ``pydantic_agent``'s own, else
            ``$RESTATE_AGENT_MODEL``, else ``"openai/gpt-6-sol"``.
        system: The instructions, when there is no ``pydantic_agent``.
        tools: ``restate.agents`` tools, Pydantic AI tools or tool functions.
        output: A Pydantic model, for answers that are one rather than
            text. The agent then answers with its JSON, which ``chat``
            returns.
        **options: The object's options, such as ``description``,
            ``background`` or ``invocation_retry_policy``: see ``AgentOptions``.

    Returns:
        The agent, to serve with ``restate.serve(restate.app([agent]))``, or any ASGI server.

    Raises:
        TypeError: If both ``pydantic_agent`` and ``system`` are given.
    """
    check_own(pydantic_agent, system)
    obj = AgentObject(name, **options)

    async def answer(ctx: SessionContext, text: str) -> Any:
        return await prompt(
            ctx,
            text,
            agent=pydantic_agent,
            model=await configured(model, ctx),
            system=await configured(system, ctx),
            tools=await configured(tools, ctx),
            output=output,
        )

    obj.chat(answer, items=ITEMS, output=output)
    return obj
