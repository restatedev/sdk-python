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
- Each message is answered once per invocation: its request and its answer are
  both marked with the prompt's id, and a retried attempt that finds them
  returns the answer, or finishes the run first.
"""

from __future__ import annotations

import typing
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from pydantic import TypeAdapter
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
    CANCELLED,
    UNANSWERED,
    ModelError,
    PerSession,
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
from restate.exceptions import TerminalError
from restate.retry_policy import InvocationRetryPolicy
from restate.sessions import Database, Migration, SessionContext
from restate.sessions.internal import Schema

_CANCELLATION = 409
"""The status code of Restate's cancellation error, which a stopped turn fails with."""

_PROMPT = "restate_agents_prompt"
"""The metadata key that marks a prompt's request with the prompt's id; models do not read it."""

_ANSWERS = "restate_agents_answers"
"""The metadata key that marks the response answering a prompt with the prompt's id."""

AnyModel: typing.TypeAlias = str | Model
"""A model: ``"<provider>/<model-id>"`` (or Pydantic AI's ``"<provider>:<model-id>"``), or a Pydantic AI model."""

AnyTool: typing.TypeAlias = Tool | PydanticTool[Any] | Callable[..., Any]
"""A tool: one made with ``restate.agents`` (``tool``, ``call``, ...), a Pydantic AI tool, or a plain function."""


def _check(own: Agent[Any, str] | None, system: object) -> None:
    if own is not None and system is not None:
        raise TypeError("an Agent of your own has its own instructions: pass no system")


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


_SCHEMA = Schema(
    "restate_agents.pydantic_ai",
    [
        """CREATE TABLE restate_pydantic_ai_messages (
            thread TEXT NOT NULL,
            n INTEGER NOT NULL,
            message TEXT NOT NULL,
            PRIMARY KEY (thread, n)
        )"""
    ],
)

# ModelMessage is an Annotated union (discriminated), which TypeAdapter takes though its type says otherwise.
_MESSAGE: TypeAdapter[ModelMessage] = TypeAdapter(typing.cast(Any, ModelMessage))


class _Conversation:
    """A conversation in the object's database, stored one message per row.

    ``save`` writes the messages that changed since the last load or save, in
    one transaction.
    """

    def __init__(self, db: Database, thread: str) -> None:
        self.db = db
        self.thread = thread
        self._saved: list[str] = []

    async def load(self) -> list[ModelMessage]:
        await _SCHEMA.ensure(self.db)
        rows = await self.db.values(
            "SELECT message FROM restate_pydantic_ai_messages WHERE thread = ? ORDER BY n", [self.thread]
        )
        self._saved = [str(row[0]) for row in rows]
        return [_MESSAGE.validate_json(m) for m in self._saved]

    async def save(self, messages: Sequence[ModelMessage]) -> None:
        dumped = [_MESSAGE.dump_json(m).decode() for m in messages]
        first = next(
            (i for i, (a, b) in enumerate(zip(dumped, self._saved, strict=False)) if a != b),
            min(len(dumped), len(self._saved)),
        )
        if first == len(dumped) == len(self._saved):
            return
        async with self.db.transaction() as tx:
            await tx.execute(
                "DELETE FROM restate_pydantic_ai_messages WHERE thread = ? AND n >= ?", [self.thread, first]
            )
            for n in range(first, len(dumped)):
                await tx.execute(
                    "INSERT INTO restate_pydantic_ai_messages VALUES (?, ?, ?)", [self.thread, n, dumped[n]]
                )
        self._saved = dumped


class _Restate(AbstractCapability[Any]):
    """Make a Pydantic AI run durable in a Restate handler.

    It stores the conversation before each model request and with each
    response, and publishes each tool call as it starts and ends for clients
    that follow the conversation. A tool's ``restate.TerminalError`` becomes its
    answer to the model; any other error fails the attempt.
    """

    def __init__(self, ctx: SessionContext, conversation: _Conversation) -> None:
        self.ctx = ctx
        self.conversation = conversation

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        await self.conversation.save(ctx.messages)
        return request_context

    async def after_model_request(
        self, ctx: RunContext[Any], *, request_context: ModelRequestContext, response: ModelResponse
    ) -> ModelResponse:
        await self.conversation.save([*ctx.messages, response])
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
        if isinstance(error, TerminalError) and error.status_code != _CANCELLATION:
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


def _prompt_of(message: ModelMessage) -> str | None:
    """Return the prompt id ``message`` is marked with, or ``None`` if it is not a prompt."""
    return (message.metadata or {}).get(_PROMPT) if isinstance(message, ModelRequest) else None


def _answer_of(history: list[ModelMessage], asked: int) -> int | None:
    """Return the index of the response that answers the prompt at index ``asked``, or ``None``."""
    return next((i for i in range(asked, len(history)) if (history[i].metadata or {}).get(_ANSWERS)), None)


def _result(part: ToolReturnPart | RetryPromptPart) -> str:
    """Return a tool call's result as the model sees it: the return value, or the retry prompt."""
    return part.model_response() if isinstance(part, RetryPromptPart) else part.model_response_str()


def _text(response: ModelMessage) -> str:
    return "".join(part.content for part in response.parts if isinstance(part, TextPart))


async def _close(conversation: _Conversation, prompt_id: str, text: str) -> None:
    """Close the cancelled, refused or failed run of prompt ``prompt_id`` with ``text``.

    Each tool call of the last response gets ``text`` as its result, and ``text``
    becomes the prompt's answer. A run that stored no prompt yet has nothing to
    close.
    """
    messages = await conversation.load()
    if not any(_prompt_of(m) == prompt_id for m in messages):
        return
    last = messages[-1]
    if isinstance(last, ModelResponse) and last.tool_calls:
        returns = [ToolReturnPart(call.tool_name, text, call.tool_call_id) for call in last.tool_calls]
        messages.append(ModelRequest(parts=returns))
    messages.append(ModelResponse(parts=[TextPart(text)], metadata={_ANSWERS: prompt_id}))
    await conversation.save(messages)


async def _answer_once(
    ctx: SessionContext,
    text: str,
    own: Agent[Any, str] | None,
    named: AnyModel | None,
    system: str | None,
    given: Sequence[AnyTool],
) -> str:
    # Resolve the model of an Agent of your own here as well, so that a missing key or package fails the same way.
    model = _model(named if named is not None else own.model if own is not None else None)
    agent: Agent[Any, str] = own or Agent(instructions=system)
    toolsets = [_toolset(given, ctx)] if given else []
    conversation = _Conversation(ctx.db, ctx.key)
    prompt_id = next_prompt_id(ctx)
    while True:
        history = await conversation.load()
        asked = next((i for i, m in enumerate(history) if _prompt_of(m) == prompt_id), None)
        if asked is not None and (answer := _answer_of(history, asked)) is not None:
            return stored_answer(_text(history[answer]))
        # Finish an interrupted run first (this prompt's own, after a crash), then start this prompt's run.
        last = next((i for i in reversed(range(len(history))) if _prompt_of(history[i]) is not None), None)
        interrupted = last is not None and _answer_of(history, last) is None
        running = _prompt_of(history[last]) if interrupted and last is not None else prompt_id
        assert running is not None
        if not interrupted:
            history = [*history, ModelRequest(parts=[UserPromptPart(text)], metadata={_PROMPT: prompt_id})]
        stop = stop_signal_of(ctx)
        run = agent.run(
            message_history=history, model=model, toolsets=toolsets, capabilities=[_Restate(ctx, conversation)]
        )
        try:
            result = await (stop.guard(run) if stop is not None else run)
        except AttemptEnded:
            # The attempt ended: stop here, and let the next attempt continue the run.
            raise
        except TerminalError as error:
            if error.status_code == _CANCELLATION:
                # Cancelled: close the run, so that the next prompt starts afresh.
                await _close(conversation, running, CANCELLED)
                raise
            # Failed for good: close the run with the error, so that the next prompt does not run into it again.
            await _close(conversation, running, f"Error: {error.message}")
            if running == prompt_id:
                raise
            continue
        except Exception as error:
            # Refused for good (a wrong API key, say): close the run unanswered and fail with a terminal error.
            # If the run was an earlier prompt's, carry on with this one.
            refused = refusal_of(ctx, error)
            if refused is None:
                raise
            await _close(conversation, running, UNANSWERED)
            if running == prompt_id:
                raise refused from error
            continue
        messages = result.all_messages()
        response = next(m for m in reversed(messages) if isinstance(m, ModelResponse))
        response.metadata = {**(response.metadata or {}), _ANSWERS: running}
        await conversation.save(messages)


async def _transcript(ctx: SessionContext) -> Transcript:
    """Return the conversation (the object's key) in the form ``messages`` returns."""
    transcript = Transcript()
    for message in await _Conversation(ctx.db, ctx.key).load():
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                transcript.say("user", part.content if isinstance(part.content, str) else "")
            elif isinstance(part, TextPart):
                transcript.say("assistant", part.content)
            elif isinstance(part, ToolCallPart):
                transcript.call(part.tool_call_id, part.tool_name, part.args_as_dict())
            elif isinstance(part, ToolReturnPart | RetryPromptPart) and part.tool_call_id:
                transcript.result(part.tool_call_id, _result(part), isinstance(part, RetryPromptPart))
    return transcript


async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    agent: Agent[Any, str] | None = None,
    model: AnyModel | None = None,
    system: str | None = None,
    tools: Sequence[AnyTool] = (),
) -> str:
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

    Returns:
        The agent's answer.

    Raises:
        TypeError: If both ``agent`` and ``system`` are given.
    """
    _check(agent, system)

    async def read() -> Transcript:
        return await _transcript(ctx)

    async def work() -> str:
        return await _answer_once(ctx, text, agent, model, system, tools)

    return await transcribing(ctx, read, work)


def agent(
    name: str,
    pydantic_agent: Agent[Any, str] | None = None,
    /,
    *,
    model: PerSession[AnyModel | None] = None,
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
        TypeError: If both ``pydantic_agent`` and ``system`` are given.
    """
    _check(pydantic_agent, system)
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
            agent=pydantic_agent,
            model=await configured(model, ctx),
            system=await configured(system, ctx),
            tools=await configured(tools, ctx),
        )

    obj.chat(answer)
    return obj
