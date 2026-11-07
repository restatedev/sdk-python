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
- Only an error of a model request can be its provider refusing (a wrong
  API key, say), which ends the turn unanswered: Pydantic AI tells them apart
  (``on_model_request_error``). Any other error fails the attempt, and
  Restate retries it, such as one of an instructions function's.
- Pydantic AI's errors that a retry would meet again end the turn for good:
  a usage limit (Pydantic AI's default is 50 model requests a run, which
  ``usage_limits`` raises: a retry would carry on with a fresh limit),
  retries that ran out (a tool's, or the answer's), a
  content filter, and a setup error (``UserError``). A response that a model
  could not read (a body that is not JSON, say) is retried.
- A run that stops for tool calls it cannot make itself (Pydantic AI's
  deferred tools: ``requires_approval``, ``CallDeferred``) fails its turn
  rather than answer with them, as nothing gives their approvals or results:
  pause for a person with ``restate.agents``' ``wait_for_signal`` tool
  instead. The turn closes the calls.
- An ``Agent`` of your own with a ``deps_type`` gets its dependencies from
  ``deps``, a value or a function of the handler's context.
- An ``Agent`` of your own keeps its system prompt (``system_prompt``),
  which Pydantic AI adds to a conversation's first request only, and keeps in
  the message history: it is stored with the conversation's first prompt,
  when the conversation's first model request is made (after any turns that
  ended before one, such as for a setup error). (``instructions`` apply to
  every request instead.) A system prompt given once a model has replied
  applies from the conversation's next ``clear``.
- Each message is answered once per invocation, by the shared conversation of
  ``restate.agents`` (``_conversation.py``), which also closes a stopped,
  refused or failed run.
"""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Unpack

from pydantic import BaseModel, ConfigDict, TypeAdapter
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ContentFilterError, UnexpectedModelBehavior, UsageLimitExceeded, UserError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestContext, infer_model
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import UsageLimits
from pydantic_core import to_json

from pydantic_ai import Agent, DeferredToolRequests, NativeOutput, PromptedOutput, RunContext
from pydantic_ai import Tool as PydanticTool
from restate.agents._common import (
    CANCELLATION,
    ErrorSet,
    ModelError,
    PerSession,
    check_own,
    configured,
    from_tool,
    model_of,
    provider_of,
)
from restate.agents._conversation import Call, Conversation, Entry, Item, Parsed, Result, Run, answer_once
from restate.agents._object import AgentObject, AgentOptions
from restate.agents._output import answer_with
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


DEFERRED = (
    "the run stopped for tool calls that need an approval or a result from outside it (Pydantic AI's deferred tools), "
    "which nothing gives here: pause for a person with restate.agents' wait_for_signal tool instead"
)
"""Why a run that ends with Pydantic AI's ``DeferredToolRequests`` fails its turn."""

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
    answer to the model; any other error fails the attempt. It records the
    errors of model requests, the only ones that can be a refusal.
    """

    def __init__(self, ctx: SessionContext, conversation: Conversation, first: Entry | None = None) -> None:
        self.ctx = ctx
        self.conversation = conversation
        self.first = first
        """The conversation's first prompt, if this run makes the conversation's first request (``_first_request``)."""
        self.model_errors = ErrorSet()
        """The errors that the run's model requests failed with."""

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
        if self.first is not None:
            # The conversation's first request, as Pydantic AI made it, with the system prompt, which it keeps in
            # the history and adds to no later request: the system prompt is stored in the conversation's first
            # prompt, so that later runs have it.
            request = next(m for m in ctx.messages if isinstance(m, ModelRequest))
            if system := _system_prompt(request):
                stored = typing.cast(ModelRequest, self.first.item)
                await self.conversation.rewrite(self.first, ModelRequest(parts=[*system, *stored.parts]))
            if earlier := [entry.item for entry in self.conversation.entries[:-1]]:
                # The model sees the conversation as it is stored: the prompts before this run's (whose runs ended
                # before any model request), with the system prompt in the first, then this run's messages. Pydantic
                # AI keeps what the hook sends as the run's history, so later requests have it too.
                messages = request_context.messages
                head = messages[0]
                if isinstance(head, ModelRequest):
                    head = dataclasses.replace(
                        head, parts=[p for p in head.parts if not isinstance(p, SystemPromptPart)]
                    )
                request_context.messages = [*earlier, head, *messages[1:]]
            self.first = None
        await self.store(ctx.messages)
        return request_context

    async def on_model_request_error(
        self, ctx: RunContext[Any], *, request_context: ModelRequestContext, error: Exception
    ) -> ModelResponse:
        self.model_errors.add(error)
        raise error

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
    except Exception as error:
        # Making the model makes no request: whatever fails is how it is set up, such as a provider's SDK whose
        # API key is not set (raising its own error before Pydantic AI 2.15 checks it first).
        raise ModelError(f'the model "{named}": {error}') from error


def _output_spec(output: type[BaseModel], model: Model) -> NativeOutput[Any] | PromptedOutput[Any]:
    """Return how to ask ``model`` for ``output``: its JSON schema mode, else in the instructions.

    Both answer with the JSON as text, which the conversation keeps as the
    reply: an output tool's call would be a tool call that ends the run.
    Gemini models before Gemini 3 have a JSON schema mode, but refuse it
    with function tools (only Google's profiles have the key), so they are
    asked in the instructions, with tools or without.
    """
    profile = model.profile
    if profile.get("supports_json_schema_output") and profile.get("google_supports_tool_combination", True):
        return NativeOutput(output)
    return PromptedOutput(output)


def _system_prompt(message: ModelMessage) -> list[SystemPromptPart]:
    """Return the system prompt parts of ``message``."""
    if not isinstance(message, ModelRequest):
        return []
    return [part for part in message.parts if isinstance(part, SystemPromptPart)]


def _first_request(conversation: Conversation) -> Entry | None:
    """Return the conversation's first prompt, if the run of the prompt it ends with makes its first request.

    That is so until a model has replied, and while no request has a system
    prompt: the conversation is the run's prompt, after any prompts whose runs
    ended before a model request (a setup error, say) and what closed them.
    Pydantic AI then adds the system prompt to the run's first request.
    """
    entries = conversation.entries
    if not entries or entries[-1] is not conversation.running():
        return None
    if any(entry.reply is not None or _system_prompt(entry.item) for entry in entries):
        return None
    return entries[0] if isinstance(entries[0].item, ModelRequest) else None


def _prompted(entry: Entry) -> Any:
    """Return what the prompt of ``entry`` asks: its text, or the content of its parts."""
    parts = [part for part in typing.cast(ModelRequest, entry.item).parts if isinstance(part, UserPromptPart)]
    if len(parts) == 1:
        return parts[0].content
    return [
        content for part in parts for content in ([part.content] if isinstance(part.content, str) else part.content)
    ]


def _run(
    ctx: SessionContext,
    own: Agent[Any, str] | None,
    named: AnyModel | None,
    system: str | None,
    given: Sequence[AnyTool],
    output: type[BaseModel] | None = None,
    deps: Any = None,
    usage_limits: UsageLimits | None = None,
) -> Run:
    """Return the run of a prompt: Pydantic AI's, from the conversation to the prompt's answer (``output``'s JSON).

    ``deps`` is the run's dependencies (Pydantic AI's ``deps``), and ``usage_limits`` its limits.
    """
    # Resolve the model of an Agent of your own here as well, so that a missing key or package fails the same way.
    model = _model(named if named is not None else own.model if own is not None else None)
    agent: Agent[Any, str] = own or Agent(instructions=system)
    toolsets = [_toolset(given, ctx)] if given else []

    async def run(conversation: Conversation) -> str:
        if (final := conversation.final_reply()) is not None:
            # The run had stored its final reply, but not its answer: that is the answer, rather than ask again.
            return final
        history = typing.cast(list[ModelMessage], [entry.item for entry in conversation.entries])
        user_prompt: Any = None
        first = _first_request(conversation)
        if first is not None:
            # The conversation's first request: Pydantic AI makes it, with the agent's system prompt, which it adds to
            # a run with no history only. `_Restate` stores the system prompt, and shows the model what came before.
            user_prompt, history = _prompted(conversation.entries[-1]), []
        restate = _Restate(ctx, conversation, first)
        spec = _output_spec(output, model) if output is not None else None
        try:
            result = await agent.run(
                user_prompt,
                message_history=history,
                model=model,
                toolsets=toolsets,
                capabilities=[restate],
                output_type=spec,
                deps=deps,
                usage_limits=usage_limits,
            )
        except UnexpectedModelBehavior as error:
            if error in restate.model_errors and not isinstance(error, ContentFilterError):
                # A model request's one response that its model could not read (a body that is not JSON, say): a
                # retry asks again.
                raise
            # Pydantic AI asked again for an answer that validates, and gave up: the reply is the answer for
            # `answer_with`, which asks once more or fails for good, rather than an error that Restate retries.
            if spec is not None and (final := conversation.final_reply()) is not None:
                return final
            # Retries that ran out (a tool's, the answer's), a content filter, a token limit: a retry would carry on
            # with fresh retries, and meet it again.
            raise TerminalError(f"the agent gave up: {error}") from error
        except UsageLimitExceeded as error:
            # A retry would carry on with a fresh limit, and so would never end an agent that keeps calling tools.
            raise TerminalError(f"the run reached its usage limit: {error}") from error
        except UserError as error:
            raise TerminalError(f"the agent is not set up right: {error}") from error
        except Exception as error:
            if error not in restate.model_errors:
                # Not a model request's: no refusal, whatever its name (an instructions function's, say).
                from_tool(ctx, error)
            raise
        # The messages that no model request stored: an output tool's return, say.
        await restate.store(result.all_messages())
        if isinstance(result.output, DeferredToolRequests):
            # Calls that wait for an approval or for a result from outside the run, which nothing gives: fail rather
            # than answer with them. The conversation closes them, as a provider refuses calls without results.
            raise TerminalError(DEFERRED)
        return result.output if isinstance(result.output, str) else to_json(result.output).decode()

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
    deps: Any = None,
    usage_limits: UsageLimits | None = None,
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
    deps: Any = None,
    usage_limits: UsageLimits | None = None,
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
    deps: Any = None,
    usage_limits: UsageLimits | None = None,
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
            text: asked for with ``NativeOutput`` if the model has a JSON
            schema mode, else with ``PromptedOutput``.
        deps: The run's dependencies (Pydantic AI's ``deps``), for an
            ``agent`` with a ``deps_type``.
        usage_limits: The run's limits (Pydantic AI's ``UsageLimits``),
            past which the turn fails. Defaults to Pydantic AI's: 50 model
            requests.

    Returns:
        The agent's answer: its text, or a value of ``output``.

    Raises:
        TypeError: If both ``agent`` and ``system`` are given.
        TerminalError: If the agent's reply is not a value of ``output``,
            twice.
    """
    check_own(agent, system)

    def ask(text: str) -> Awaitable[str]:
        return answer_once(ctx, text, ITEMS, _run(ctx, agent, model, system, tools, output, deps, usage_limits))

    if output is None:
        return await ask(text)
    return await answer_with(output, text, ask)


def agent(
    name: str,
    pydantic_agent: Agent[Any, str] | None = None,
    /,
    *,
    model: PerSession[AnyModel | None] = None,
    system: PerSession[str | None] = None,
    tools: PerSession[Sequence[AnyTool]] = (),
    output: type[BaseModel] | None = None,
    deps: PerSession[Any] = None,
    usage_limits: UsageLimits | None = None,
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

    An ``Agent`` of your own with a ``deps_type`` gets its dependencies from
    ``deps``, a value or a function of the invocation's context::

        assistant = pydantic_ai.agent("assistant", shop, deps=lambda ctx: Customer(ctx.key))

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
            text: asked for with ``NativeOutput`` if the model has a JSON
            schema mode, else with ``PromptedOutput``. ``chat`` returns its
            JSON.
        deps: The runs' dependencies (Pydantic AI's ``deps``), for a
            ``pydantic_agent`` with a ``deps_type``: a value, or a function of
            the invocation's context.
        usage_limits: Each run's limits (Pydantic AI's ``UsageLimits``),
            past which its turn fails. Defaults to Pydantic AI's: 50 model
            requests.
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
            deps=await configured(deps, ctx),
            usage_limits=usage_limits,
        )

    obj.chat(answer, items=ITEMS, output=output)
    return obj
