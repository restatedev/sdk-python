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
  ``deps``: a value, or for ``agent`` also a function of the handler's
  context.
- The built-in agent gets Pydantic AI's own capability for a conversation
  that lives long: the compaction of its model's provider, where Pydantic AI
  has one (``AnthropicCompaction``, and ``OpenAICompaction`` for OpenAI's
  Responses API, which ``openai/...`` models use). It summarizes the
  history as it nears the model's context window, which a long conversation
  would otherwise outgrow, failing each turn. A model that its provider does
  not compact (Anthropic's before the 4.6 models, say) refuses such requests:
  run it as an ``Agent`` of your own, which runs as you configured it. Give
  that the capabilities it needs, such as
  ``Agent(model, capabilities=[AnthropicCompaction()])``.
- The conversation stores no system prompt: Pydantic AI's
  ``ReinjectSystemPrompt`` gives each run the system prompt of an ``Agent``
  of your own (``system_prompt``), as it is now. (``instructions`` apply to
  every request anyway.)
- Each message is answered once per invocation, by the shared conversation of
  ``restate.agents`` (``_conversation.py``), which also closes a stopped,
  refused or failed run.
"""

from __future__ import annotations

import typing
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Unpack

from pydantic import BaseModel, ConfigDict, TypeAdapter
from pydantic_ai.capabilities import AbstractCapability, ReinjectSystemPrompt
from pydantic_ai.exceptions import ContentFilterError, UnexpectedModelBehavior, UsageLimitExceeded, UserError
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
from pydantic_ai.usage import UsageLimits
from pydantic_core import to_json

from pydantic_ai import Agent, DeferredToolRequests, NativeOutput, PromptedOutput, RunContext
from pydantic_ai import Tool as PydanticTool
from restate.agents._common import (
    CANCELLATION,
    ErrorSet,
    ModelError,
    PerSession,
    check_built_in,
    check_own,
    configured,
    from_tool,
    model_of,
    provider_of,
)
from restate.agents._conversation import Call, Conversation, Item, Parsed, Result, Run, answer_once
from restate.agents._object import AgentObject, AgentOptions
from restate.agents._output import answer_with
from restate.agents._tools import Tool, current_call, tools_in
from restate.agents._turn import stop_signal_of, tool_call_ended, tool_call_started
from restate.exceptions import TerminalError
from restate.sessions import SessionContext

M = typing.TypeVar("M", bound=BaseModel)

AnyModel: typing.TypeAlias = str | Model
"""A model: ``"<provider>/<model-id>"`` (or Pydantic AI's ``"<provider>:<model-id>"``), or a Pydantic AI model."""

AnyTool: typing.TypeAlias = Tool | PydanticTool[Any] | Callable[..., Any]
"""A tool: one made with ``restate.agents`` (``tool``, ``call``, ...), a Pydantic AI tool, or a plain function."""

_Capability: typing.TypeAlias = AbstractCapability[Any]


def toolset(given: Sequence[AnyTool]) -> FunctionToolset[Any]:
    """Return ``given`` as a Pydantic AI toolset, for an ``Agent`` of your own::

        desk = Agent(model, toolsets=[pydantic_ai.toolset([call(refund)])])

    ``restate.agents`` tools get their tool call (and the handler's context)
    from the run of ``agent`` or ``prompt`` in progress, and validate their own
    arguments; Pydantic AI tools and tool functions are passed through.

    Args:
        given: ``restate.agents`` tools, and Pydantic AI tools and tool functions.

    Returns:
        The tools, as a toolset of Pydantic AI.
    """

    def native(tool: Tool) -> PydanticTool[Any]:
        async def run(run_ctx: RunContext[Any], **args: Any) -> str:
            return await tool.run(args, current_call(run_ctx.tool_call_id or ""))

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

    def __init__(self, ctx: SessionContext, conversation: Conversation) -> None:
        self.ctx = ctx
        self.conversation = conversation
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


def _compaction(model: Model) -> _Capability | None:
    """Return the compaction of ``model``'s provider, if Pydantic AI has one: Anthropic's, and the Responses API's."""
    try:
        if model.system == "anthropic":
            from pydantic_ai.models.anthropic import (  # pylint: disable=import-outside-toplevel
                AnthropicCompaction,
                AnthropicModel,
            )

            return AnthropicCompaction() if isinstance(model, AnthropicModel) else None
        if model.system == "openai":
            from pydantic_ai.models.openai import (  # pylint: disable=import-outside-toplevel
                OpenAICompaction,
                OpenAIResponsesModel,
            )

            return OpenAICompaction() if isinstance(model, OpenAIResponsesModel) else None
    except ImportError:
        pass
    return None


def _capabilities(own: Agent[Any, Any] | None, model: Model) -> list[_Capability]:
    """Return the capabilities that a run gets beside the agent's own.

    The built-in agent (``own`` is ``None``) gets the compaction of its
    model's provider, for a conversation that lives long; an Agent of your
    own runs as you configured it. Either gets ``ReinjectSystemPrompt``,
    unless the agent has one: the conversation stores no system prompt,
    which Pydantic AI keeps in the history it is given.
    """
    have = {type(capability) for capability in (own.root_capability.capabilities if own is not None else ())}
    chosen = [compaction] if own is None and (compaction := _compaction(model)) is not None else []
    if ReinjectSystemPrompt not in have:
        chosen.append(ReinjectSystemPrompt())
    return chosen


def _run(
    ctx: SessionContext,
    own: Agent[Any, Any] | None,
    named: AnyModel | None,
    system: str | None,
    given: Sequence[AnyTool],
    output: type[BaseModel] | None = None,
    deps: Any = None,
    usage_limits: UsageLimits | None = None,
) -> Run:
    """Return the run of a prompt: Pydantic AI's, from the conversation to the prompt's answer (``output``'s JSON).

    ``deps`` is the run's dependencies (Pydantic AI's ``deps``), and ``usage_limits`` its limits: options of a run,
    which an ``Agent`` cannot hold.
    """
    # Resolve the model of an Agent of your own here as well, so that a missing key or package fails the same way.
    model = _model(named if named is not None else own.model if own is not None else None)
    agent: Agent[Any, Any] = own or Agent(instructions=system)
    toolsets = [toolset(given)] if given else []
    chosen = _capabilities(own, model)

    async def run(conversation: Conversation) -> str:
        if (final := conversation.final_reply()) is not None:
            # The run had stored its final reply, but not its answer: that is the answer, rather than ask again.
            return final
        history = typing.cast(list[ModelMessage], [entry.item for entry in conversation.entries])
        restate = _Restate(ctx, conversation)
        spec = _output_spec(output, model) if output is not None else None
        try:
            # The tools of ``toolset`` find their call and the handler's context in the run.
            with tools_in(ctx):
                result = await agent.run(
                    message_history=history,
                    model=model,
                    toolsets=toolsets,
                    capabilities=[restate, *chosen],
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
    model: AnyModel | None = None,
    system: str | None = None,
    tools: Sequence[AnyTool] = (),
    output: None = None,
    usage_limits: UsageLimits | None = None,
) -> str: ...


@typing.overload
async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    model: AnyModel | None = None,
    system: str | None = None,
    tools: Sequence[AnyTool] = (),
    output: type[M],
    usage_limits: UsageLimits | None = None,
) -> M: ...


@typing.overload
async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    agent: Agent[Any, Any],
    output: None = None,
    deps: Any = None,
    usage_limits: UsageLimits | None = None,
) -> str: ...


@typing.overload
async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    agent: Agent[Any, Any],
    output: type[M],
    deps: Any = None,
    usage_limits: UsageLimits | None = None,
) -> M: ...


async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    agent: Agent[Any, Any] | None = None,
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

    As ``agent`` does, it takes the common options of the built-in agent, or
    an ``Agent`` of your own, configured::

        return await pydantic_ai.prompt(ctx, text, agent=desk)

    Args:
        ctx: The handler's context.
        text: The prompt.
        agent: A Pydantic AI ``Agent`` of your own, configured with its
            model, instructions, tools (``toolset`` for this package's) and
            capabilities, instead of ``model``, ``system`` and ``tools``.
            One with no model gets the built-in agent's default.
        model: The built-in agent's model: ``"<provider>/<model-id>"`` of any
            of Pydantic AI's providers, or a Pydantic AI model. Defaults to
            ``$RESTATE_AGENT_MODEL``, else ``"openai/gpt-6-sol"``.
        system: The built-in agent's instructions.
        tools: The built-in agent's tools: ``restate.agents`` tools,
            Pydantic AI tools or tool functions.
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
        TypeError: If ``agent`` is given with ``model``, ``system`` or
            ``tools``, or ``deps`` without ``agent``.
        TerminalError: If the agent's reply is not a value of ``output``,
            twice.
    """
    check_own(agent, "agent", model=model, system=system, tools=tools)
    check_built_in(agent, "agent", deps=deps)
    return await _prompt(ctx, text, agent, model, system, tools, output, deps, usage_limits)


async def _prompt(
    ctx: SessionContext,
    text: str,
    own: Agent[Any, Any] | None,
    model: AnyModel | None,
    system: str | None,
    tools: Sequence[AnyTool],
    output: type[BaseModel] | None,
    deps: Any,
    usage_limits: UsageLimits | None,
) -> Any:
    """Answer ``text`` as ``prompt`` does, with its options checked."""

    def ask(text: str) -> Awaitable[str]:
        return answer_once(ctx, text, ITEMS, _run(ctx, own, model, system, tools, output, deps, usage_limits))

    if output is None:
        return await ask(text)
    return await answer_with(output, text, ask)


@typing.overload
def agent(
    name: str,
    *,
    model: PerSession[AnyModel | None] = None,
    system: PerSession[str | None] = None,
    tools: PerSession[Sequence[AnyTool]] = (),
    output: type[BaseModel] | None = None,
    usage_limits: UsageLimits | None = None,
    **options: Unpack[AgentOptions],
) -> AgentObject: ...


@typing.overload
def agent(
    name: str,
    *,
    agent: Agent[Any, Any],
    output: type[BaseModel] | None = None,
    deps: PerSession[Any] = None,
    usage_limits: UsageLimits | None = None,
    **options: Unpack[AgentOptions],
) -> AgentObject: ...


def agent(
    name: str,
    *,
    agent: Agent[Any, Any] | None = None,
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
    handlers of your own with ``@assistant.handler``.

    The built-in agent takes the common options, and gets Pydantic AI's
    compaction for a conversation that lives long::

        assistant = pydantic_ai.agent("assistant", system="Answer briefly.", tools=[get_weather])

    ``model``, ``system`` and ``tools`` may each be a function of the
    invocation's context, sync or async, for values that differ per user::

        assistant = pydantic_ai.agent("assistant", system=lambda ctx: f"You help {ctx.key}.")

    For anything more, pass an ``Agent`` of your own, configured as Pydantic
    AI does: its model, instructions or system prompt, tools (``toolset`` for
    this package's), capabilities. It runs as you configured it::

        desk = Agent(model, system_prompt="...", toolsets=[pydantic_ai.toolset([call(refund)])])
        assistant = pydantic_ai.agent("assistant", agent=desk, deps=lambda ctx: Customer(ctx.key))

    Args:
        name: The object's name, as Restate serves it.
        agent: A Pydantic AI ``Agent`` of your own, configured, instead of
            ``model``, ``system`` and ``tools``. One with no model gets the
            built-in agent's default.
        model: The built-in agent's model: ``"<provider>/<model-id>"`` of any
            of Pydantic AI's providers (each needs its extra, such as
            ``pydantic-ai-slim[openai]``), or a Pydantic AI model. Defaults to
            ``$RESTATE_AGENT_MODEL``, else ``"openai/gpt-6-sol"``.
        system: The built-in agent's instructions.
        tools: The built-in agent's tools: ``restate.agents`` tools,
            Pydantic AI tools or tool functions.
        output: A Pydantic model, for answers that are one rather than
            text: asked for with ``NativeOutput`` if the model has a JSON
            schema mode, else with ``PromptedOutput``. ``chat`` returns its
            JSON.
        deps: The runs' dependencies (Pydantic AI's ``deps``), for an
            ``agent`` with a ``deps_type``: a value, or a function of the
            invocation's context.
        usage_limits: Each run's limits (Pydantic AI's ``UsageLimits``),
            past which its turn fails. Defaults to Pydantic AI's: 50 model
            requests.
        **options: The object's options, such as ``description``,
            ``background`` or ``invocation_retry_policy``: see ``AgentOptions``.

    Returns:
        The agent, to serve with ``restate.serve(restate.app([agent]))``, or any ASGI server.

    Raises:
        TypeError: If ``agent`` is given with ``model``, ``system`` or
            ``tools``, or ``deps`` without ``agent``.
    """
    check_own(agent, "agent", model=model, system=system, tools=tools)
    check_built_in(agent, "agent", deps=deps)
    own = agent
    obj = AgentObject(name, **options)

    async def answer(ctx: SessionContext, text: str) -> Any:
        return await _prompt(
            ctx,
            text,
            own,
            await configured(model, ctx),
            await configured(system, ctx),
            await configured(tools, ctx),
            output,
            await configured(deps, ctx),
            usage_limits,
        )

    obj.chat(answer, items=ITEMS, output=output)
    return obj
