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
- A tool call that Pydantic AI defers (``requires_approval``,
  ``ApprovalRequired``, ``CallDeferred``, the harness's ``AskUser`` with no
  answerer) waits for a signal, durably (``wait_for``): ``approval`` for an
  approval, else a signal named after the tool, whose value is the call's
  result. The agent's ``pending`` view lists the waits and ``signal``
  resolves them; no process is held meanwhile, a retried attempt joins the
  same wait, and ``stop`` forgets it. An approval runs the tool only if the
  value says so (``approval_of``); any other text denies it, as the reason
  the model gets. A capability of the agent may resolve the calls first
  (``ApprovalReviewer``, say).
- An ``Agent`` of your own with a ``deps_type`` gets its dependencies from
  ``deps``: a value, or for ``agent`` also a function of the handler's
  context.
- The model gets the conversation since the latest compaction, whole, rather
  than the turn projection of the other frameworks (``window`` and
  ``budget`` do not apply): the harness's compaction rewrites the history
  as it nears the model's context window, and the rewritten history is
  stored as a snapshot of the turn before the request goes out (``Turn.snapshot``),
  without system prompts. The next turn, or a retried attempt, starts from
  it, so the history only grows between compactions and the provider's
  prompt cache holds. The views show the original steps, never a snapshot.
  (``prompt`` in a session object that is no Pydantic AI agent gets the turn
  projection: its runs compact, but store no snapshot.)
- The compaction is the harness's recommended ``TieredCompaction``
  (``compaction``: clamp oversized parts, clear old tool results, then
  summarize; within 80% of the window), for the built-in agent and for an
  ``Agent`` of your own that has no compaction capability of its own; one
  that has runs as configured. Near the target (70%), a turn has the
  history compacted after its answer (the ``compact`` handler, with
  ``compact_now``), so the next turn starts compacted. A model whose window
  neither its profile nor ``genai-prices`` knows is taken as 200,000
  tokens: pass ``context_window``.
- The conversation stores no system prompt: Pydantic AI's
  ``ReinjectSystemPrompt`` gives each run the system prompt of an ``Agent``
  of your own (``system_prompt``), as it is now. (``instructions`` apply to
  every request anyway.)
- Every run of a key has the conversation id ``<agent>/<key>`` (a run's own
  id is the attempt's), which the harness's ``ConversationSearch`` and
  ``WarnOnCacheBusts`` go by.
- The stores of the harness's capabilities can be the key's database
  (``_stores.py``): ``plan_store`` for ``Planning``, ``overflow_store`` for
  ``ToolOutputLimits``, ``history_source`` for ``ConversationSearch``. A
  tool call's writes are done once, however often it runs; ``clear``
  empties them.
- Usage limits (``usage_limits``) are each run's, so each turn's.
- A message steered to a turn (the agent's ``steer``) joins it at its next
  step: once the model's tool calls all have their results stored, before
  the next model request (``take_steers``). It is stored as a message of the
  user that is a step of the turn, then given to the model with Pydantic
  AI's ``RunContext.enqueue``, into that request and the run's history,
  after what they hold. The run carries on with it (its usage limits,
  retries and capabilities' state too), and a retried attempt, or the next
  turn, has the message where the model saw it. A message that comes after
  the turn's last step (while the model gives its final answer, say) is
  answered next, as a turn of its own.
- Each message is answered once per invocation, by the shared conversation of
  ``restate.agents`` (``_conversation.py``), which also closes a stopped,
  refused or failed run.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import json
import sys
import typing
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Unpack

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering, ReinjectSystemPrompt
from pydantic_ai.exceptions import ContentFilterError, UnexpectedModelBehavior, UsageLimitExceeded, UserError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryFeedbackPart,
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
from pydantic_ai_harness.ask_user import TOOL_NAME as ASK_USER
from pydantic_ai_harness.ask_user import AskUserRequest, AskUserResponse, ask_user_result
from pydantic_ai_harness.compaction import (
    DEFAULT_CONTEXT_WINDOW,
    ClampOversizedMessages,
    ClearToolResults,
    CompactOnRequest,
    DeduplicateFileReads,
    FallbackCompaction,
    RelevanceCompaction,
    SlidingWindowCompaction,
    SummarizingCompaction,
    TieredCompaction,
    compact_now,
    estimate_context_tokens,
    resolve_context_window,
)
from pydantic_core import to_json, to_jsonable_python

import pydantic_ai
from pydantic_ai import (
    Agent,
    DeferredToolRequests,
    DeferredToolResults,
    NativeOutput,
    PromptedOutput,
    RunContext,
    ToolApproved,
    ToolDenied,
)
from pydantic_ai import Tool as PydanticTool
from restate.agents._common import (
    Check,
    CompactHistory,
    ErrorSet,
    ModelError,
    PerSession,
    check_built_in,
    check_own,
    configured,
    from_tool,
    is_cancellation,
    model_of,
    provider_of,
)
from restate.agents._conversation import Call, Conversation, Item, Parsed, Result, Run, answer_once
from restate.agents._object import AgentObject, AgentOptions
from restate.agents._output import answer_with
from restate.agents._steering import take_steers
from restate.agents._tools import Tool, current_call, tools_in, wait_for
from restate.agents._turn import stop_signal_of, tool_call_started
from restate.agents.pydantic_ai._stores import TurnsHistorySource, calling, conversation_id_of
from restate.exceptions import TerminalError
from restate.sessions import SessionContext

# Pydantic AI shows a banner on a process's first agent run, unless an installed
# package started the run. Ours start in a Restate handler, behind the event
# loop, where it cannot tell: turn it off, as it advises a library that runs
# agents on its users' behalf.
pydantic_ai.BANNER_ENABLED = False

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
    "which a capability of the agent left unresolved"
)
"""Why a run that ends with Pydantic AI's ``DeferredToolRequests`` fails its turn: its deferred calls wait for a
signal (``APPROVAL``, or the tool's name), unless a capability before resolves them otherwise."""

APPROVAL = "approval"
"""The signal that a tool call waits for when it needs an approval (Pydantic AI's ``requires_approval``,
``ApprovalRequired``)."""

_APPROVED = frozenset({"approved", "approve", "yes", "ok", "allow", "allowed"})
"""The texts that approve a tool call, whatever their case; any other text denies it, with that reason."""

_DENIED = frozenset({"", "denied", "deny", "no", "rejected", "reject", "declined", "decline", "refused", "refuse"})
"""The texts that deny a tool call with no reason of their own, whatever their case: the model is told it was denied."""


def approval_of(value: Any) -> ToolApproved | ToolDenied:
    """Return what the value of an ``approval`` signal decides about its tool call.

    ``true``, or a text such as ``"approved"`` or ``"yes"``, approves it;
    ``{"approved": true, "args": {...}}`` approves it with other arguments.
    ``false`` or ``{"approved": false, "reason": "..."}`` denies it, and so
    does any other text, which is the denial's reason for the model: a value
    that does not say "approved" never runs the tool. A text that only says
    "no" (``"denied"``, ``"rejected"``...) tells the model the call was denied.
    """
    if value is True:
        return ToolApproved()
    if isinstance(value, str):
        said = value.strip().lower()
        if said in _APPROVED:
            return ToolApproved()
        return ToolDenied() if said in _DENIED else ToolDenied(value)
    if isinstance(value, dict):
        decision = typing.cast(dict[str, Any], value)
        if decision.get("approved") is True:
            args = decision.get("args")
            return ToolApproved(override_args=typing.cast(dict[str, Any], args) if isinstance(args, dict) else None)
        reason = decision.get("reason", decision.get("message"))
        return ToolDenied(str(reason)) if reason else ToolDenied()
    return ToolDenied()


def _answered(call: ToolCallPart, value: object) -> object:
    """Return the value of a signal for ``call``, a deferred call, as its result: an answer to the harness's
    ``AskUser`` question that fits it (an ``AskUserResponse``), as ``AskUser`` renders one."""
    if call.tool_name != ASK_USER:
        return value
    try:
        return ask_user_result(AskUserRequest.from_tool_call(call), _ASK_USER_RESPONSE.validate_python(value))
    except (ValueError, ValidationError):
        # Not an answer to the questions (a text, say): the model gets it as it is.
        return value


_ASK_USER_RESPONSE: TypeAdapter[AskUserResponse] = TypeAdapter(AskUserResponse)

# ModelMessage is an Annotated union (discriminated), which TypeAdapter takes though its type says otherwise.
# Bytes (a tool's binary result, say) as base64, as pydantic-ai's own ModelMessagesTypeAdapter stores them.
_MESSAGE: TypeAdapter[ModelMessage] = TypeAdapter(
    typing.cast(Any, ModelMessage), config=ConfigDict(ser_json_bytes="base64", val_json_bytes="base64")
)


def _result(part: ToolReturnPart) -> str:
    """Return a tool call's result as the model sees it: the return value, or why to call it again."""
    return part.model_response_str()


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

    def reply(self, text: str, agent: str | None = None) -> Item:
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
            Result(part.tool_call_id, _result(part), part.outcome != "success")
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_call_id
        ]
        said = ("user", "".join(p if isinstance(p, str) else "" for p in prompts)) if prompts else None
        return Parsed(said, results=results)


ITEMS = _Items()


def _feedback_of(message: ModelMessage) -> list[str]:
    """Return Pydantic AI's feedback on a reply it could not use (an answer that does not validate, say) that
    ``message`` holds, which answers no tool call: each ``RetryFeedbackPart``, as JSON.

    A part is its own feedback (it has its own time): a run keeps it as it is when it merges consecutive requests
    (a feedback and a message steered after it, say) or resumes from the last one, though the request is new.
    """
    if not isinstance(message, ModelRequest):
        return []
    return [to_json(p, bytes_mode="base64").decode() for p in message.parts if isinstance(p, RetryFeedbackPart)]


class _Restate(AbstractCapability[Any]):
    """Make a Pydantic AI run durable in a Restate handler.

    It stores the requests before each model request and each response as it
    arrives (each write that commits publishes, for clients that follow the
    conversation), and settles the earlier calls as each tool call starts. A
    tool's ``restate.TerminalError`` becomes its
    answer to the model; any other error fails the attempt. It records the
    errors of model requests, the only ones that can be a refusal.
    """

    def __init__(self, ctx: SessionContext, conversation: Conversation, history: Sequence[ModelMessage]) -> None:
        self.ctx = ctx
        self.conversation = conversation
        self.model_errors = ErrorSet()
        """The errors that the run's model requests failed with."""
        self.feedback = {feedback for m in history for feedback in _feedback_of(m)}
        """Pydantic AI's feedback that the conversation has (each ``RetryFeedbackPart``), as JSON."""

    def _new_feedback(self, message: ModelMessage) -> bool:
        # Found by its parts, not the whole request: the run's request may be a stored one, merged or resumed.
        return any(feedback not in self.feedback for feedback in _feedback_of(message))

    async def store(self, messages: Sequence[ModelMessage]) -> None:
        """Store the requests in ``messages`` (the run's) that the conversation does not have yet.

        Those are the tools' results, and Pydantic AI's feedback on a reply it
        could not use (``RetryFeedbackPart``): the rest of what a run adds, its
        responses, is stored as each arrives. Results are found by their call
        ids, and feedback by its parts, not by their place or their request: a
        history processor (``ProcessHistory``) may have trimmed the run's
        messages, and a run merges the consecutive requests it is given (a
        message steered after a result, say) and resumes from the last one as
        a request of its own. What it makes of them is the model's to see, not
        the conversation's.
        """
        results = {id(m) for m in self.conversation.new_results(messages)}
        new = [m for m in messages if id(m) in results or self._new_feedback(m)]
        if new:
            await self.conversation.turn.append(*new)
            self.feedback.update(feedback for m in new for feedback in _feedback_of(m))

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
        await self.conversation.turn.append_reply(response)
        return response

    async def before_tool_execute(
        self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition, args: Any
    ) -> Any:
        # The stores of this package write once per tool call (``_stores.py``).
        calling(call.tool_call_id)
        await tool_call_started(self.ctx)
        return args

    async def after_tool_execute(
        self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition, args: Any, result: Any
    ) -> Any:
        calling(None)
        return result

    async def handle_deferred_tool_calls(
        self, ctx: RunContext[Any], *, requests: DeferredToolRequests
    ) -> DeferredToolResults | None:
        """Wait for each deferred call's signal (``wait_for``): ``approval``, or the result of a call that runs
        outside, as a signal named after its tool.

        The run waits without holding a process (a retried attempt joins the
        same waits), the agent's ``pending`` view lists them, and ``signal``
        resolves them; ``stop`` forgets them.
        """

        def shown(call: ToolCallPart) -> dict[str, Any]:
            return {"tool": call.tool_name, "args": call.args_as_dict()}

        async def approval(call: ToolCallPart) -> tuple[str, ToolApproved | ToolDenied]:
            value = await wait_for(self.ctx, call.tool_call_id, APPROVAL, shown(call), approval=True)
            return call.tool_call_id, approval_of(value)

        async def result(call: ToolCallPart) -> tuple[str, object]:
            value = await wait_for(self.ctx, call.tool_call_id, call.tool_name, shown(call))
            return call.tool_call_id, _answered(call, value)

        approvals, calls = await asyncio.gather(
            asyncio.gather(*map(approval, requests.approvals)), asyncio.gather(*map(result, requests.calls))
        )
        return DeferredToolResults(approvals=dict(approvals), calls=dict(calls))

    async def on_tool_execute_error(
        self, ctx: RunContext[Any], *, call: ToolCallPart, tool_def: ToolDefinition, args: Any, error: Exception
    ) -> Any:
        calling(None)
        stop = stop_signal_of(self.ctx)
        if stop is not None and stop.reason is not None:
            raise stop.reason from error
        if isinstance(error, TerminalError) and not is_cancellation(error):
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
        # Making the model makes no request: whatever fails is how it is set up, such as a missing API key
        # (Pydantic AI's UserError), or a provider's SDK whose client cannot be made (its own error).
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


def _compacts(capability: _Capability) -> bool:
    """Return whether ``capability`` compacts the history: one of the harness's strategies (or any with its
    ``compact``), or a provider's compaction."""
    # A provider's compaction is in its model's module, which needs the provider's SDK: an agent has one only if the
    # module is loaded.
    providers: tuple[type[Any], ...] = tuple(
        getattr(module, name)
        for module_name, name in _PROVIDER_COMPACTIONS
        if (module := sys.modules.get(module_name)) is not None
    )
    kinds: tuple[type[Any], ...] = (*_STRATEGIES, *providers)
    return isinstance(capability, kinds) or callable(getattr(capability, "compact", None))


_PROVIDER_COMPACTIONS = (
    ("pydantic_ai.models.anthropic", "AnthropicCompaction"),
    ("pydantic_ai.models.openai", "OpenAICompaction"),
)
"""The compactions of model providers that Pydantic AI has, by module."""


_STRATEGIES = (
    ClampOversizedMessages,
    ClearToolResults,
    CompactOnRequest,
    DeduplicateFileReads,
    FallbackCompaction,
    RelevanceCompaction,
    SlidingWindowCompaction,
    SummarizingCompaction,
    TieredCompaction,
)
"""The harness's capabilities that compact the history."""

TARGET = 0.8
"""The share of the model's context window that the default compaction keeps the history within."""

BETWEEN = 0.7
"""The share of the model's context window past which a turn has the history compacted after its answer, so that the
next turns start compacted rather than compact before a model request (``TARGET``)."""


def compaction(context_window: int | None = None, *, target: float = TARGET) -> TieredCompaction[Any]:
    """Return the compaction a Pydantic AI agent gets by default: the harness's recommended ``TieredCompaction``.

    Before each model request whose history is over 80% of the model's
    context window, it clamps oversized parts, then clears old tool results
    (keeping the last three), and only then has the model summarize the
    history but its latest 20 messages, stopping as soon as the history fits.

    Args:
        context_window: The model's context window, in tokens, for a model whose window neither its profile nor
            the registry of ``genai-prices`` knows (or knows wrong). By default, the model's, else 200,000.
        target: The share of the context window to keep the history within.
    """
    return TieredCompaction(
        tiers=[
            ClampOversizedMessages(max_part_tokens=50_000),
            ClearToolResults(max_tokens=1, keep_pairs=3),
            SummarizingCompaction(max_messages=1, keep_messages=20),
        ],
        target_fraction=target,
        context_window=context_window,
    )


def _leaves(own: Agent[Any, Any] | None) -> list[_Capability]:
    """Return the capabilities of ``own``, an Agent of your own, nested ones included."""
    leaves: list[_Capability] = []
    if own is not None:
        own.root_capability.apply(leaves.append)
    return leaves


def _capabilities(own: Agent[Any, Any] | None, context_window: int | None) -> list[_Capability]:
    """Return the capabilities that a run gets beside the agent's own.

    The built-in agent (``own`` is ``None``) gets the default compaction
    (``compaction``), and so does an Agent of your own that has none of its
    own: either history then stays within the model's context window. Either
    gets ``ReinjectSystemPrompt``, unless the agent has one: the conversation
    stores no system prompt, which Pydantic AI keeps in the history it is
    given.
    """
    leaves = _leaves(own)
    chosen: list[_Capability] = [] if any(map(_compacts, leaves)) else [compaction(context_window)]
    if not any(isinstance(capability, ReinjectSystemPrompt) for capability in leaves):
        chosen.append(ReinjectSystemPrompt())
    return chosen


def _part_print(part: Any) -> str:
    dumped: object = to_jsonable_python(part, bytes_mode="base64")
    if isinstance(dumped, dict):
        dumped = {k: v for k, v in typing.cast(dict[str, object], dumped).items() if k != "timestamp"}
    return json.dumps(dumped, sort_keys=True)


def _prints(messages: Sequence[ModelMessage]) -> list[tuple[str, list[str]]]:
    """Return what of ``messages`` a rewrite of the history changes: their parts but system prompts, without times.

    Consecutive messages of one side are one, their parts in any order: a
    run starts by merging consecutive requests (and putting tool results
    first), which changes the shape of the history it was given, not what
    it says.
    """
    printed: list[tuple[str, list[str]]] = []
    for message in messages:
        parts = [_part_print(p) for p in message.parts if not isinstance(p, SystemPromptPart)]
        if printed and printed[-1][0] == message.kind:
            printed[-1] = (message.kind, sorted([*printed[-1][1], *parts]))
        else:
            printed.append((message.kind, sorted(parts)))
    return printed


def _extends(now: list[tuple[str, list[str]]], before: list[tuple[str, list[str]]]) -> bool:
    """Return whether the history ``now`` extends ``before`` (both as ``_prints`` has them), rather than rewrites it.

    It extends it if it starts with it, the last message of ``before``
    perhaps with more parts: Pydantic AI records state in the latest request
    (the instructions in force, say).
    """
    if not before:
        return True
    if len(now) < len(before) or now[: len(before) - 1] != before[:-1]:
        return False
    (kind, parts), (then, had) = now[len(before) - 1], before[-1]
    return kind == then and not collections.Counter(had) - collections.Counter(parts)


def _unprompted(message: ModelMessage) -> ModelMessage:
    """Return ``message`` without its system prompts, which ``ReinjectSystemPrompt`` gives each run as it is now."""
    if isinstance(message, ModelRequest) and any(isinstance(p, SystemPromptPart) for p in message.parts):
        return dataclasses.replace(message, parts=[p for p in message.parts if not isinstance(p, SystemPromptPart)])
    return message


def _unanchored(message: ModelMessage) -> ModelMessage:
    """Return ``message`` without what its usage says of its request's input, if it is a response.

    The harness estimates a history's tokens from the latest response's input
    tokens, which measured the request it answered: in a compacted history,
    that is the history before the compaction, until a response comes after
    it. Without them, the history is measured as it is now (an attempt that
    carries on from the snapshot, or the turn after ``compact``), rather than
    compacted again.
    """
    if not isinstance(message, ModelResponse) or not message.usage.input_tokens:
        return message
    usage = dataclasses.replace(
        message.usage,
        input_tokens=0,
        cache_write_tokens=0,
        cache_write_1h_tokens=0,
        cache_read_tokens=0,
        input_audio_tokens=0,
        cache_audio_read_tokens=0,
    )
    return dataclasses.replace(message, usage=usage)


def _snapshot_of(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    """Return ``messages``, compacted, as a snapshot keeps them: without system prompts, nor input usage (see
    ``_unanchored``)."""
    return [_unanchored(_unprompted(m)) for m in messages]


class _Snapshots(AbstractCapability[Any]):
    """Store the history as a snapshot of the turn when a compaction rewrote it, before the request goes out.

    It runs after every other capability's ``before_model_request`` but
    ``_Steering``'s (it is innermost), so it sees the messages as they are
    sent. The history was rewritten, rather than extended, if the messages
    sent before (or, for a run's first request, the history the run was
    given) do not start them: the next turn, or a retried attempt, then
    starts from the snapshot.
    """

    def __init__(self, conversation: Conversation, history: Sequence[ModelMessage]) -> None:
        self.conversation = conversation
        self.sent = _prints(history)
        """What the model was given last (at first, the history the run was given), as ``_prints`` has it."""

    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position="innermost")

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        messages = request_context.messages
        prints = _prints(messages)
        if not _extends(prints, self.sent):
            await self.conversation.turn.snapshot(_snapshot_of(messages))
        self.sent = prints
        return request_context


class _Steering(AbstractCapability[Any]):
    """Give the model the messages steered to the turn at its next step, in the request about to go out.

    Before each model request, it takes the messages waiting
    (``take_steers``), which stores each once as a step of the turn, and
    hands them to Pydantic AI's own ``RunContext.enqueue`` (``'asap'``): once
    every ``before_model_request`` ran, Pydantic AI adds them to this request
    and to the run's history, after what both hold. The run goes on with them
    (its usage limits, retries and capabilities' state too); a retried attempt
    and the next turns find them in their history, where the model saw them,
    and never take them again.

    It is the innermost capability, after ``_Snapshots``: a snapshot that this
    request stores covers what was stored before the messages, so every
    history after it has them after it.
    """

    def __init__(self, ctx: SessionContext, conversation: Conversation) -> None:
        self.ctx = ctx
        self.conversation = conversation

    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position="innermost", wrapped_by=[_Snapshots])

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        messages = request_context.messages
        if not messages or isinstance(messages[-1], ModelResponse):
            # A response that its provider paused, resumed: Pydantic AI delivers nothing into its request, so a
            # message taken now would reach the model after the response, though stored before it.
            return request_context
        if steered := typing.cast(list[ModelRequest], await take_steers(self.ctx, self.conversation)):
            ctx.enqueue(*steered, priority="asap")
        return request_context


def _due(messages: list[ModelMessage], model: Model, context_window: int | None) -> bool:
    """Return whether ``messages`` are near the default compaction's target for ``model`` (past ``BETWEEN``)."""
    window = context_window or resolve_context_window(model) or DEFAULT_CONTEXT_WINDOW
    return estimate_context_tokens(messages) > window * BETWEEN


def history_source() -> TurnsHistorySource:
    """Return the conversation's turns as a source for the harness's ``ConversationSearch``::

        desk = Agent(model, capabilities=[ConversationSearch(pydantic_ai.history_source())])

    Each turn is a run: its id is the turn's prompt id, its start the time
    the turn was asked, and its messages the turn's as stored, the originals
    of what a compaction summarized for the model. The runs are those of the
    conversation (its id is ``<agent>/<key>``), so the default scope finds
    them.
    """
    return TurnsHistorySource(ITEMS)


def _run(
    ctx: SessionContext,
    own: Agent[Any, Any] | None,
    named: AnyModel | None,
    system: str | None,
    given: Sequence[AnyTool],
    output: type[BaseModel] | None = None,
    deps: Any = None,
    usage_limits: UsageLimits | None = None,
    context_window: int | None = None,
) -> Run:
    """Return the run of a prompt: Pydantic AI's, from the conversation to the prompt's answer (``output``'s JSON).

    ``deps`` is the run's dependencies (Pydantic AI's ``deps``), and ``usage_limits`` its limits: options of a run,
    which an ``Agent`` cannot hold. ``context_window`` is the default compaction's (``compaction``).
    """
    # Resolve the model of an Agent of your own here as well, so that a missing key or package fails the same way.
    model = _model(named if named is not None else own.model if own is not None else None)
    agent: Agent[Any, Any] = own or Agent(instructions=system)
    toolsets = [toolset(given)] if given else []
    chosen = _capabilities(own, context_window)
    compacted = any(isinstance(capability, TieredCompaction) for capability in chosen)
    conversation_id = conversation_id_of(ctx)

    async def run(conversation: Conversation) -> str:
        if (final := conversation.turn.final_reply()) is not None:
            # The run had stored its final reply, but not its answer: that is the answer, rather than ask again.
            return final
        history = typing.cast(list[ModelMessage], conversation.history())
        restate = _Restate(ctx, conversation, history)
        # The snapshots of an agent that takes its history from them (the agent's ``chat``); a prompt of a session
        # object that is no Pydantic AI agent takes the turn projection, which never reads one.
        snapshots = [_Snapshots(conversation, history)] if conversation.snapshots else []
        # The messages steered to a turn of a ``chat`` that takes them, with snapshots or without; any other run takes
        # none (``take_steers``).
        steering = _Steering(ctx, conversation)
        spec = _output_spec(output, model) if output is not None else None
        try:
            # The tools of ``toolset`` find their call and the handler's context in the run.
            with tools_in(ctx):
                result = await agent.run(
                    message_history=history,
                    model=model,
                    toolsets=toolsets,
                    capabilities=[restate, *chosen, *snapshots, steering],
                    output_type=spec,
                    deps=deps,
                    usage_limits=usage_limits,
                    conversation_id=conversation_id,
                )
        except UnexpectedModelBehavior as error:
            if error in restate.model_errors and not isinstance(error, ContentFilterError):
                # A model request's one response that its model could not read (a body that is not JSON, say): a
                # retry asks again.
                raise
            # Pydantic AI asked again for an answer that validates, and gave up: the reply is the answer for
            # `answer_with`, which asks once more or fails for good, rather than an error that Restate retries.
            if spec is not None and (final := conversation.turn.final_reply()) is not None:
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
            # Calls that a capability left waiting for an approval or for a result from outside the run: fail rather
            # than answer with them. The conversation closes them, as a provider refuses calls without results.
            raise TerminalError(DEFERRED)
        if compacted and conversation.snapshots and _due(result.all_messages(), model, context_window):
            # Near the target: compact after the answer (``compact``), rather than before a later model request.
            conversation.compact_after()
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
    context_window: int | None = None,
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
    context_window: int | None = None,
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
    context_window: int | None = None,
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
    context_window: int | None = None,
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
    context_window: int | None = None,
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
        context_window: The model's context window, in tokens, for the
            default compaction (``compaction``), if neither the model's
            profile nor the registry of ``genai-prices`` knows it (or knows
            it wrong). Without it, an unknown window is taken as 200,000.

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
    return await _prompt(ctx, text, agent, model, system, tools, output, deps, usage_limits, context_window)


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
    context_window: int | None,
) -> Any:
    """Answer ``text`` as ``prompt`` does, with its options checked."""
    run = _run(ctx, own, model, system, tools, output, deps, usage_limits, context_window)

    def ask(text: str, check: Check | None = None) -> Awaitable[str]:
        return answer_once(ctx, text, ITEMS, run, check=check)

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
    context_window: int | None = None,
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
    context_window: int | None = None,
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
    context_window: int | None = None,
    **options: Unpack[AgentOptions],
) -> AgentObject:
    """Create a Pydantic AI agent: a virtual object with one conversation per key.

    Its ``chat`` handler answers each message with Pydantic AI; the
    conversation is kept in the key's database, a step at a time. A message
    sent to its ``steer`` view while a turn runs joins the turn at its next
    step, once the model's tool calls have their results. Add handlers of
    your own with ``@assistant.handler``.

    The built-in agent takes the common options, and gets the harness's
    compaction (``compaction``) for a conversation that lives long::

        assistant = pydantic_ai.agent("assistant", system="Answer briefly.", tools=[get_weather])

    ``model``, ``system`` and ``tools`` may each be a function of the
    invocation's context, sync or async, for values that differ per user::

        assistant = pydantic_ai.agent("assistant", system=lambda ctx: f"You help {ctx.key}.")

    For anything more, pass an ``Agent`` of your own, configured as Pydantic
    AI does: its model, instructions or system prompt, tools (``toolset`` for
    this package's), capabilities. It runs as you configured it, with the
    default compaction if it has none of its own::

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
        context_window: The model's context window, in tokens, for the
            default compaction (``compaction``), if neither the model's
            profile nor the registry of ``genai-prices`` knows it (or knows
            it wrong). Without it, an unknown window is taken as 200,000.
        **options: The object's options, such as ``description``,
            ``background`` or ``invocation_retry_policy``: see
            ``AgentOptions``. ``window`` and ``budget`` do not apply: the
            model gets the history since the latest compaction, whole.

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
            context_window,
        )

    compact_history = None if any(map(_compacts, _leaves(own))) else _compactor(own, model, context_window)
    # Its runs take the messages steered to a turn at the turn's steps (``_Steering``).
    obj.chat(answer, items=ITEMS, output=output, snapshots=True, compact_history=compact_history, steers=True)
    return obj


def _compactor(
    own: Agent[Any, Any] | None, model: PerSession[AnyModel | None], context_window: int | None
) -> CompactHistory:
    """Return how an agent with the default compaction compacts its history between turns: with ``compact_now``.

    A turn asks for it once its history is near the compaction's target
    (``BETWEEN``), so that the next turns start compacted rather than compact
    before a model request (``_run``). It compacts to ``BETWEEN``.
    """

    async def compact_history(ctx: SessionContext, history: list[Any]) -> list[Any] | None:
        named = await configured(model, ctx) if own is None else own.model
        messages = typing.cast(list[ModelMessage], history)
        compacted = await compact_now(
            compaction(context_window, target=BETWEEN),
            messages,
            model=_model(named),
            conversation_id=conversation_id_of(ctx),
        )
        return None if compacted == messages else _snapshot_of(compacted)

    return compact_history
