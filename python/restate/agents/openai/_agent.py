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
"""OpenAI Agents SDK agents whose conversations are stored in the object's database, one step at a time.

The SDK's ``Runner`` runs the agent, on the shared conversation of
``restate.agents`` (``_conversation.py``), which answers each prompt once and
closes a stopped, refused or failed run. The handler is not replayed: a
retried attempt (after a crash or a lost connection) runs again from the
start, and the run resumes from the last step stored:

- Each model reply is stored as it arrives (``RunHooks.on_llm_end``), before
  its tools run, and the tools' results before the next request
  (``call_model_input_filter``) or once the run ends. A crash while tools are
  running runs them again with the same call ids, which a tool can pass on to
  stay idempotent. (The SDK's own ``Session`` stores a step only once its
  tools have run.)
- A reply that cannot be resumed that way is dropped, and its agent is asked
  again (with new call ids): one with a call that only the SDK's runner can
  make (a handoff, a hosted tool, a function tool with guardrails, which a
  resumed call would bypass). A function tool's timeout applies to a resumed
  call too. A call to a tool the agent does not have (which the SDK fails the
  run for) is reported back to the model to fix, on the retry.
- A run that stops for tool calls that need an approval (``needs_approval``)
  fails its turn, rather than answer without them, as nothing gives the
  approval: pause for a person with ``restate.agents``' ``wait_for_signal``
  tool instead. The turn closes the calls, which never run. A cut-off reply
  with such a call fails its turn the same way, without asking again (a
  function tool's ``needs_approval`` function decides, as in the run).
- The agent a handoff gave the conversation to answers the next messages too.
  The stored conversation is the full transcript: a handoff passes it on as it
  is, rather than collapsed into one message (``nest_handoff_history`` is off),
  and a handoff's ``input_filter`` changes only what the next agent sees.
- A tool's error fails the attempt, and Restate retries it. A
  ``restate.TerminalError``, which a retry would only repeat, is instead the
  tool's answer to the model.
- A guardrail's tripwire fails the prompt for good, and the reply it blocked is
  dropped from the conversation. A stored reply becomes the answer without
  asking again only if its agent has no output guardrails.
- Only an error of a model request can be its provider refusing (a wrong API
  key, say), which ends the turn unanswered: the run's hooks mark each
  request, from the input filter's end (when the SDK has run the
  instructions and the run's own filter, and goes on to make the model and
  its client, which needs the key) to ``on_llm_end``. Any other error fails
  the attempt, and Restate retries it, such as one of an instructions
  function's.
- The SDK's setup errors (its ``UserError``: an unknown model prefix, say) and
  a package the agent needs that is not installed (``litellm``, say) end the
  turn for good: a retry would meet them again.
- A run of more turns than ``max_turns`` (the SDK's default is 10) fails its
  prompt for good: a retry would carry on with a fresh count.
- An ``Agent`` of your own runs as you configured it, with the ``context``
  (the SDK's run context) and the ``run_config`` you give: its
  ``call_model_input_filter`` runs after this package's, which stores the
  tools' outputs.
- The model sees the earlier turns as their messages and answers, and the
  latest turns whole (the agent's ``window``), as many as its ``budget``
  holds; the built-in agent has its model summarize the earliest once they
  outgrow it, after a turn's answer (``_conversation.py``).
- The built-in agent gets OpenAI's server-side compaction, for a conversation
  that lives long (``ModelSettings.context_management``, on versions of the
  SDK that have it): the Responses API summarizes the history as it nears the
  model's context window, which a long conversation would otherwise outgrow,
  failing each turn. Its compaction item is stored like any reply, and a
  request starts with the latest one: it stands for the items before it,
  which OpenAI's guide says need not be sent again (as the SDK's own
  compacting session drops them), so that requests stop growing with the
  conversation, which is stored whole. A model of the Chat Completions API,
  or of another provider, gets
  none, and a model that OpenAI does not compact refuses such requests: run
  it as an ``Agent`` of your own, configured as you need.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import typing
from collections.abc import Awaitable, Sequence
from typing import Any, Unpack

from agents import (
    Agent,
    AgentOutputSchema,
    FunctionTool,
    Handoff,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    Model,
    ModelResponse,
    ModelSettings,
    OpenAIResponsesModel,
    OutputGuardrailTripwireTriggered,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    UserError,
)
from agents import Tool as SdkTool
from agents.run import CallModelData, ModelInputData
from agents.tool_context import ToolContext
from pydantic import BaseModel
from pydantic_core import to_json

from restate.agents._common import (
    PerSession,
    Summarize,
    check_built_in,
    check_own,
    configured,
    from_tool,
    model_id_of,
    model_of,
    raised_by_tool,
)
from restate.agents._conversation import Call, Conversation, Item, Parsed, Result, Run, answer_once
from restate.agents._object import AgentObject, AgentOptions
from restate.agents._output import answer_with
from restate.agents._tools import Tool, current_call, tools_in
from restate.agents._turn import tool_call_ended, tool_call_started
from restate.exceptions import TerminalError
from restate.sessions import SessionContext

M = typing.TypeVar("M", bound=BaseModel)


AnyModel: typing.TypeAlias = str | Model
"""A model: ``"openai/<model-id>"`` (or the model id alone), or a model of the SDK."""

AnyTool: typing.TypeAlias = Tool | SdkTool
"""A tool: one made with ``restate.agents`` (``tool``, ``call``, ``wait_for_signal``, ...), or a tool of the SDK."""


# --- The SDK's items ------------------------------------------------------------------------------


def _text(item: Item) -> str:
    """Return the text of a message item."""
    content = item.get("content")
    if isinstance(content, str):
        return content
    parts = typing.cast(list[dict[str, Any]], content or [])
    return "".join(str(part.get("text", "")) for part in parts)


def _output(value: Any) -> str:
    return value if isinstance(value, str) else to_json(value).decode()


class _Items:
    """The Responses API's input items, as the SDK keeps a conversation: dicts, stored as JSON."""

    def dump(self, item: Item) -> str:
        return json.dumps(item)

    def load(self, stored: str) -> Item:
        return json.loads(stored)

    def prompt(self, text: str) -> Item:
        return {"role": "user", "content": text}

    def reply(self, text: str, agent: str | None = None) -> Item:
        return {"role": "assistant", "content": text}

    def results(self, results: Sequence[tuple[Call, str]]) -> list[Item]:
        return [{"type": "function_call_output", "call_id": call.id, "output": text} for call, text in results]

    def read(self, item: Item) -> Parsed:
        kind = str(item.get("type", "message"))
        if kind == "message" and item.get("role") in ("user", "assistant"):
            return Parsed(said=(str(item["role"]), _text(item)))
        if kind == "function_call":
            arguments = str(item.get("arguments") or "{}")
            try:
                input: Any = json.loads(arguments)
            except ValueError:
                input = arguments
            return Parsed(calls=[Call(str(item["call_id"]), str(item["name"]), input)])
        if kind.endswith("_output") and "call_id" in item:
            # The output of any kind of tool: function_call_output, shell_call_output...
            output = item.get("output")
            return Parsed(
                results=[Result(str(item["call_id"]), output if isinstance(output, str) else json.dumps(output))]
            )
        return Parsed()


ITEMS = _Items()


# --- The agents -----------------------------------------------------------------------------------


def _agents(root: Agent[Any]) -> dict[str, Agent[Any]]:
    """Return the agents that ``root`` and its handoffs reach, by name."""
    found: dict[str, Agent[Any]] = {}
    waiting = [root]
    while waiting:
        agent = waiting.pop()
        if agent.name in found:
            continue
        found[agent.name] = agent
        for handoff in agent.handoffs:
            target = handoff if isinstance(handoff, Agent) else getattr(handoff, "_agent_ref", lambda: None)()
            if isinstance(target, Agent):
                waiting.append(typing.cast(Agent[Any], target))
    return found


def _handoffs(agent: Agent[Any]) -> dict[str, str]:
    """Return the agents that ``agent`` hands off to, by the name of the tool that hands off to each."""
    targets: dict[str, str] = {}
    for handoff in agent.handoffs:
        if isinstance(handoff, Handoff):
            targets[handoff.tool_name] = handoff.agent_name
        else:
            targets[Handoff.default_tool_name(handoff)] = handoff.name
    return targets


def _current(conversation: Conversation, root: Agent[Any], everyone: dict[str, Agent[Any]]) -> Agent[Any]:
    """Return the agent the conversation is with: the latest reply's, or the agent that reply handed off to.

    With no reply loaded (the window holds none), it is the agent the latest answered turn left it with.
    """
    latest = next((e for e in reversed(conversation.entries) if e.reply is not None), None)
    if latest is None or latest.reply is None:
        return everyone.get(conversation.carried()[0] or "", root)
    agent = everyone.get(latest.agent or "", root)
    targets = _handoffs(agent)
    for call in (call for e in conversation.reply_entries(latest.reply) for call in e.calls):
        if call.name in targets and conversation.has_result(call.id):
            return everyone.get(targets[call.name], agent)
    return agent


# --- A run ----------------------------------------------------------------------------------------


class _Hooks(RunHooks[Any]):
    """Store each step of a run as it comes, and publish each tool call as it starts and ends."""

    def __init__(self, ctx: SessionContext, conversation: Conversation, config: RunConfig) -> None:
        self.ctx = ctx
        self.conversation = conversation
        self.then = config.call_model_input_filter
        """The run's own ``call_model_input_filter``, if it has one."""
        self.requesting = False
        """Whether a model request is in progress, with the making of its model and client: the only time an error
        can be its provider refusing."""

    async def model_input(self, data: CallModelData[Any]) -> ModelInputData:
        """The run's ``call_model_input_filter``: store the tools' outputs before the next request.

        The run's own filter then changes what the model sees, not what is stored.
        """
        if outputs := self.conversation.new_results(data.model_data.input):
            await self.conversation.append(*outputs)
        filtered = data.model_data
        if self.then is not None:
            given = self.then(data)
            filtered = await given if inspect.isawaitable(given) else given
        # The SDK goes on to make the model and its client (which needs the API key: a refusal if it is missing), and
        # to request it. (The instructions have run already.)
        self.requesting = True
        return filtered

    async def on_llm_end(self, context: RunContextWrapper[Any], agent: Agent[Any], response: ModelResponse) -> None:
        self.requesting = False
        reply = typing.cast(list[Item], response.to_input_items())
        await self.conversation.append_reply(*reply, agent=agent.name)

    async def on_tool_start(self, context: RunContextWrapper[Any], agent: Agent[Any], tool: SdkTool) -> None:
        await tool_call_started(self.ctx)

    async def on_handoff(self, context: RunContextWrapper[Any], from_agent: Agent[Any], to_agent: Agent[Any]) -> None:
        # The turn carries the agent it handed off to, even if it ends before that agent replies.
        self.conversation.carry(agent=to_agent.name)

    async def on_tool_end(self, context: RunContextWrapper[Any], agent: Agent[Any], tool: SdkTool, result: Any) -> None:
        await tool_call_ended(self.ctx)


async def _resume(
    ctx: SessionContext, conversation: Conversation, agent: Agent[Any], everyone: dict[str, Agent[Any]], context: Any
) -> Agent[Any]:
    """Resume from a reply whose tool calls have no outputs (cut off by a crash), and return the agent to run.

    Function tools of the reply's agent run again with their call ids, and a
    call to a tool that agent does not have gets an error for the model to act
    on. A reply with any other call (a handoff, say) is dropped instead, and
    its agent is asked again.
    """
    calls = conversation.unanswered()
    if not calls:
        return agent
    cut = next(e for e in conversation.entries if calls[0] in e.calls)
    agent = everyone.get(cut.agent or "", agent)
    found = {tool.name: tool for tool in await agent.get_all_tools(RunContextWrapper(context))}
    known = {*found, *_handoffs(agent)}
    approvals = [await _approval(found.get(call.name), call, context) for call in calls]
    if any(approval is True for approval in approvals):
        # A call that waits for an approval, which nothing gives: fail as the run would have, rather than run it, or
        # ask again (which would run the reply's other tools again, with new call ids). The conversation closes them.
        raise TerminalError(APPROVALS)
    undecided = any(approval is None for approval in approvals)
    if undecided or any(call.name in known and _runner_only(found.get(call.name)) for call in calls):
        # A call that only the SDK's runner can make (a handoff, a hosted tool, a tool with guardrails), or decide on
        # (another tool's approval function): ask the agent again instead.
        if cut.reply is not None:
            await conversation.drop(cut.reply)
        return agent
    outputs: list[Item] = []
    for call in calls:
        tool = found.get(call.name)
        if not isinstance(tool, FunctionTool):
            output = f"Error: there is no tool named {call.name!r}"
        else:
            arguments = call.input if isinstance(call.input, str) else json.dumps(call.input)
            given = ToolContext(context, tool_name=tool.name, tool_call_id=call.id, tool_arguments=arguments)
            await tool_call_started(ctx)
            try:
                output = _output(await _invoke(tool, given, arguments))
            except Exception as error:
                # Mark the error as the tool's, so that it is never mistaken for the model provider refusing.
                from_tool(ctx, error)
                raise
            await tool_call_ended(ctx)
        outputs.extend(ITEMS.results([(call, output)]))
    await conversation.append(*outputs)
    return agent


async def _approval(tool: SdkTool | None, call: Call, context: Any) -> bool | None:
    """Return whether ``call`` (to ``tool``) needs an approval, or ``None`` if only the SDK's runner can tell.

    A function tool's ``needs_approval`` function is asked, with the call's
    arguments and id, as the runner asks it. Another tool's (a shell's, say)
    takes other arguments.
    """
    needs = getattr(tool, "needs_approval", False)
    if not callable(needs):
        return bool(needs)
    arguments: object = call.input
    if not isinstance(tool, FunctionTool) or not isinstance(arguments, dict):
        return None
    decided = needs(RunContextWrapper(context), typing.cast(dict[str, Any], arguments), call.id)
    return bool(await decided if inspect.isawaitable(decided) else decided)


def _runner_only(tool: SdkTool | None) -> bool:
    """Return whether only the SDK's runner can make a call to ``tool``.

    That is so for any tool but a function tool (a handoff has none), and for
    a function tool with guardrails, which the runner applies around it, and
    calling the tool itself would bypass. (Older versions of the SDK have no
    tool guardrails.)
    """
    if not isinstance(tool, FunctionTool):
        return True
    return any(getattr(tool, name, None) for name in ("tool_input_guardrails", "tool_output_guardrails"))


_INVOKE: Any = getattr(importlib.import_module("agents.tool"), "invoke_function_tool", None)
"""The SDK's invocation of a function tool, which applies its timeout (versions of the SDK with tool timeouts)."""


async def _invoke(tool: FunctionTool, context: ToolContext[Any], arguments: str) -> Any:
    """Run a call to ``tool`` as the SDK's runner does, with its timeout, though not its guardrails or approval."""
    if _INVOKE is None:
        return await tool.on_invoke_tool(context, arguments)
    return await _INVOKE(function_tool=tool, context=context, arguments=arguments)


SETUP = "the agent is not set up right"
"""Why a run fails its turn for the SDK's setup error, which a retry would meet again."""

APPROVALS = (
    "the run stopped for tool calls that need an approval (the SDK's needs_approval), which nothing gives here: "
    "pause for a person with restate.agents' wait_for_signal tool instead"
)
"""Why a run that stops for approvals (the SDK's interruptions) fails its turn."""


async def _unwrapped(ctx: SessionContext, work: Awaitable[Any]) -> Any:
    """Await ``work``, raising a tool's error as the tool raised it rather than as the SDK wraps it.

    Errors that a retry would meet again become terminal: too many turns, the
    SDK's setup errors, and a package that is not installed (but for a
    tool's, which may be passing).
    """
    try:
        return await work
    except UserError as error:
        cause = error.__cause__
        if isinstance(cause, Exception):
            # A tool's error, which the SDK wraps: raise it as the tool raised it, marked as the tool's, so that it is
            # never mistaken for the model provider refusing.
            from_tool(ctx, cause)
            raise cause from None
        # The SDK's own (an unknown model prefix, an input filter that returns no ModelInputData...).
        raise TerminalError(f"{SETUP}: {error}") from error
    except ImportError as error:
        if raised_by_tool(ctx, error):
            raise
        # A package that the agent needs (its model provider's, say).
        raise TerminalError(f"{SETUP}: {error}") from error
    except MaxTurnsExceeded as error:
        raise TerminalError(str(error)) from error


def _output_schema(output: type[BaseModel]) -> AgentOutputSchema:
    """Return ``output`` as the SDK's output type: strict if its JSON schema can be, as OpenAI's models answer best."""
    try:
        return AgentOutputSchema(output, strict_json_schema=True)
    except UserError:
        return AgentOutputSchema(output, strict_json_schema=False)


@dataclasses.dataclass(frozen=True)
class _Options:
    """The options of a run that an ``Agent`` cannot hold."""

    context: Any = None
    """The SDK's run context."""
    max_turns: int | None = None
    """The most turns of the run, else the SDK's default."""
    config: RunConfig | None = None
    """The run's ``RunConfig``."""


def _run(
    ctx: SessionContext,
    own: Agent[Any] | None,
    model: AnyModel | None,
    system: str | None,
    given: Sequence[AnyTool],
    output: type[BaseModel] | None,
    options: _Options,
) -> Run:
    """Return the run of a prompt: the ``Runner``, from the conversation to the prompt's answer (``output``'s JSON).

    ``output`` is the output type of the agent the run starts with. An agent
    it hands off to answers with its own output type, as text if it has none.
    """
    if own is None:
        chosen = _model(model)
        settings = _long_lived(chosen)
        root: Agent[Any] = Agent(name="assistant", instructions=system, tools=tools(given), model_settings=settings)
        config = RunConfig(model=chosen, nest_handoff_history=False)
    else:
        root = own
        # The conversation is the full transcript, which a handoff passes on as it is.
        config = dataclasses.replace(options.config or RunConfig(), nest_handoff_history=False)
    if output is not None:
        root = root.clone(output_type=_output_schema(output))
    everyone = _agents(root)

    async def run(conversation: Conversation) -> str:
        reply = conversation.last_reply()
        if reply is not None and (final := conversation.final_reply()) is not None:
            replier = everyone.get(conversation.agent_of(reply) or "", root)
            if not (replier.output_guardrails or config.output_guardrails):
                # The run had stored its final reply, but not its answer: that is the answer.
                return final
            # Its output guardrails may not have passed it: ask again, and they run on the new reply.
            await conversation.drop(reply)
        with tools_in(ctx):
            return await _go(ctx, conversation, _current(conversation, root, everyone), everyone, config, options)

    return run


async def _go(
    ctx: SessionContext,
    conversation: Conversation,
    current: Agent[Any],
    everyone: dict[str, Agent[Any]],
    config: RunConfig,
    options: _Options,
) -> str:
    """Run the ``Runner`` on from the conversation, with the agent it is with, and return the answer."""
    hooks = _Hooks(ctx, conversation, config)
    filtered = dataclasses.replace(config, call_model_input_filter=hooks.model_input)
    # The SDK's default, unless given: older versions take no None.
    limit: dict[str, Any] = {"max_turns": options.max_turns} if options.max_turns is not None else {}
    try:
        agent = await _unwrapped(ctx, _resume(ctx, conversation, current, everyone, options.context))
        items = typing.cast(Any, _since_compaction(conversation.history()))
        run = Runner.run(agent, items, context=options.context, hooks=hooks, run_config=filtered, **limit)
        result = await _unwrapped(ctx, run)
    except (InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered) as error:
        # The reply the guardrail blocked is not kept.
        if (blocked := conversation.last_reply()) is not None:
            await conversation.drop(blocked)
        raise TerminalError(str(error)) from error
    except TerminalError:
        raise
    except Exception as error:
        if not hooks.requesting:
            # Not a model request's: no refusal, whatever its name (an instructions function's, say).
            from_tool(ctx, error)
        raise
    # The outputs that no model request stored: those of a tool that ends the turn, say.
    if outputs := conversation.new_results(result.to_input_list()):
        await conversation.append(*outputs)
    if getattr(result, "interruptions", None):
        # Calls that wait for an approval, which nothing gives: fail rather than answer without them. The
        # conversation closes them, so that they never run.
        raise TerminalError(APPROVALS)
    return _output(result.final_output)


def _since_compaction(items: list[Item]) -> list[Item]:
    """Return ``items`` from the latest compaction item on (OpenAI's), which stands for those before it."""
    latest = max((i for i, item in enumerate(items) if item.get("type") == "compaction"), default=0)
    return items[latest:]


def tools(given: Sequence[AnyTool]) -> list[SdkTool]:
    """Return ``given`` as tools of the SDK, for an ``Agent`` of your own and the agents it hands off to::

        refunds = Agent(name="refunds", tools=openai.tools([call(refund)]))

    Args:
        given: ``restate.agents`` tools, and tools of the SDK (passed through).

    Returns:
        The tools, as the SDK takes them.
    """
    return [_function_tool(t) if isinstance(t, Tool) else t for t in given]


def _function_tool(tool: Tool) -> FunctionTool:
    """Return a ``restate.agents`` tool as a function tool of the SDK, which gets its call's id."""

    async def invoke(context: ToolContext[Any], arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
        except ValueError as error:
            return f"Error: the arguments are not JSON: {error}"
        return await tool.run(args, current_call(context.tool_call_id))

    return FunctionTool(
        name=tool.name,
        description=tool.description,
        params_json_schema=tool.input_schema,
        on_invoke_tool=invoke,
        strict_json_schema=False,
    )


COMPACTION: list[Any] = [{"type": "compaction"}]
"""OpenAI's server-side compaction, at its default threshold, as Pydantic AI's ``OpenAICompaction`` asks for it."""


def _long_lived(model: str | Model) -> ModelSettings:
    """Return the built-in agent's model settings, for a conversation that lives long.

    A model of the Responses API (a model id, which the SDK's default
    provider makes one of, or such a model) gets OpenAI's server-side
    compaction, on versions of the SDK that have it (``context_management``);
    any other model, none.
    """
    has_it = "context_management" in {field.name for field in dataclasses.fields(ModelSettings)}
    if has_it and isinstance(model, str | OpenAIResponsesModel):
        return ModelSettings(context_management=COMPACTION)
    return ModelSettings()


def _model(model: AnyModel | None) -> str | Model:
    """Return a model of the SDK, or the model id of ``openai/<model-id>``; raise ``ModelError`` for another provider."""
    return model if isinstance(model, Model) else model_id_of(model_of(model), "openai")


# --- The API --------------------------------------------------------------------------------------


@typing.overload
async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    model: AnyModel | None = None,
    system: str | None = None,
    tools: Sequence[AnyTool] = (),
    output: None = None,
    max_turns: int | None = None,
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
    max_turns: int | None = None,
) -> M: ...


@typing.overload
async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    agent: Agent[Any],
    output: None = None,
    context: Any = None,
    max_turns: int | None = None,
    run_config: RunConfig | None = None,
) -> str: ...


@typing.overload
async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    agent: Agent[Any],
    output: type[M],
    context: Any = None,
    max_turns: int | None = None,
    run_config: RunConfig | None = None,
) -> M: ...


async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    agent: Agent[Any] | None = None,
    model: AnyModel | None = None,
    system: str | None = None,
    tools: Sequence[AnyTool] = (),
    output: type[BaseModel] | None = None,
    context: Any = None,
    max_turns: int | None = None,
    run_config: RunConfig | None = None,
) -> Any:
    """Answer a prompt with an OpenAI Agents SDK agent, exactly once.

    For a handler of your own; the conversation is the object's key, shared
    with its ``chat``. A retried attempt resumes the same prompt rather than
    asking again, and each prompt of an invocation is answered on its own::

        @assistant.handler
        async def summarize(ctx: sessions.SessionContext, topic: str) -> str:
            return await openai.prompt(ctx, f"Summarize what we said about {topic}")

    As ``agent`` does, it takes the common options of the built-in agent, or
    an ``Agent`` of your own, configured::

        return await openai.prompt(ctx, text, agent=triage)

    Args:
        ctx: The handler's context.
        text: The prompt.
        agent: An ``Agent`` of your own (with handoffs, guardrails, ...),
            configured with its model, instructions and tools (``tools()``
            for this package's), instead of ``model``, ``system`` and
            ``tools``.
        model: The built-in agent's model: ``"openai/<model-id>"``, or a
            model of the SDK. Defaults to ``$RESTATE_AGENT_MODEL``, else
            ``"openai/gpt-6-sol"``.
        system: The built-in agent's instructions.
        tools: The built-in agent's tools: ``restate.agents`` tools, or
            tools of the SDK.
        output: A Pydantic model, for an answer that is one rather than
            text: the output type of the agent the run starts with. An agent
            it hands off to that answers in text is asked again for its JSON.
        context: The run context of ``agent`` (the SDK's ``context``), which
            its tools and instructions functions get.
        max_turns: The most turns of the run, past which the prompt fails.
            Defaults to the SDK's: 10.
        run_config: The ``RunConfig`` of ``agent``'s run (a model for every
            agent, a model provider, tracing, guardrails...). Its
            ``nest_handoff_history`` is off: the conversation is the full
            transcript.

    Returns:
        The agent's answer: its text, or a value of ``output``.

    Raises:
        TypeError: If ``agent`` is given with ``model``, ``system`` or
            ``tools``, or ``context`` or ``run_config`` without ``agent``.
        TerminalError: If the agent's reply is not a value of ``output``,
            twice.
    """
    check_own(agent, "agent", model=model, system=system, tools=tools)
    check_built_in(agent, "agent", context=context, run_config=run_config)
    options = _Options(context, max_turns, run_config)
    return await _prompt(ctx, text, agent, model, system, tools, output, options)


async def _prompt(
    ctx: SessionContext,
    text: str,
    own: Agent[Any] | None,
    model: AnyModel | None,
    system: str | None,
    tools: Sequence[AnyTool],
    output: type[BaseModel] | None,
    options: _Options,
) -> Any:
    """Answer ``text`` as ``prompt`` does, with its options checked."""

    def ask(text: str) -> Awaitable[str]:
        return answer_once(ctx, text, ITEMS, _run(ctx, own, model, system, tools, output, options))

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
    max_turns: int | None = None,
    **options: Unpack[AgentOptions],
) -> AgentObject: ...


@typing.overload
def agent(
    name: str,
    *,
    agent: Agent[Any],
    output: type[BaseModel] | None = None,
    context: PerSession[Any] = None,
    max_turns: int | None = None,
    run_config: RunConfig | None = None,
    **options: Unpack[AgentOptions],
) -> AgentObject: ...


def agent(
    name: str,
    *,
    agent: Agent[Any] | None = None,
    model: PerSession[AnyModel | None] = None,
    system: PerSession[str | None] = None,
    tools: PerSession[Sequence[AnyTool]] = (),
    output: type[BaseModel] | None = None,
    context: PerSession[Any] = None,
    max_turns: int | None = None,
    run_config: RunConfig | None = None,
    **options: Unpack[AgentOptions],
) -> AgentObject:
    """Create an OpenAI Agents SDK agent: a virtual object with one conversation per key.

    Its ``chat`` handler answers each message with the SDK's ``Runner``; the
    conversation is kept in the key's database, a step at a time. Add
    handlers of your own with ``@assistant.handler``.

    The built-in agent takes the common options::

        assistant = openai.agent("assistant", system="Answer briefly.", tools=[get_weather])

    ``model``, ``system`` and ``tools`` may each be a function of the
    invocation's context, sync or async, for values that differ per user::

        assistant = openai.agent("assistant", system=lambda ctx: f"You help {ctx.key}.")

    For anything more, pass an ``Agent`` of your own, configured as the SDK
    does: handoffs, guardrails, tools (``tools()`` for this package's), and a
    ``RunConfig`` for its runs. It runs as you configured it::

        triage = Agent(name="triage", handoffs=[refunds], tools=openai.tools([call(lookup)]))
        assistant = openai.agent("desk", agent=triage, context=lambda ctx: Account(ctx.key))

    Args:
        name: The object's name, as Restate serves it.
        agent: An ``Agent`` of your own, configured, instead of ``model``,
            ``system`` and ``tools``.
        model: The built-in agent's model: ``"openai/<model-id>"``, or a
            model of the SDK. Defaults to ``$RESTATE_AGENT_MODEL``, else
            ``"openai/gpt-6-sol"``.
        system: The built-in agent's instructions.
        tools: The built-in agent's tools: ``restate.agents`` tools, or
            tools of the SDK.
        output: A Pydantic model, for answers that are one rather than
            text: the output type of the agent a run starts with. An agent
            it hands off to that answers in text is asked again for its
            JSON. ``chat`` returns the JSON.
        context: The run context of ``agent`` (the SDK's ``context``): a
            value, or a function of the invocation's context.
        max_turns: The most turns of a run, past which its message fails.
            Defaults to the SDK's: 10.
        run_config: The ``RunConfig`` of ``agent``'s runs (a model for every
            agent, a model provider, tracing, guardrails...). Its
            ``nest_handoff_history`` is off: the conversation is the full
            transcript.
        **options: The object's options, such as ``description``,
            ``background`` or ``invocation_retry_policy``: see ``AgentOptions``.

    Returns:
        The agent, to serve with ``restate.serve(restate.app([agent]))``, or any ASGI server.

    Raises:
        TypeError: If ``agent`` is given with ``model``, ``system`` or
            ``tools``, or ``context`` or ``run_config`` without ``agent``.
        TerminalError: If ``model`` names a provider other than OpenAI.
    """
    check_own(agent, "agent", model=model, system=system, tools=tools)
    check_built_in(agent, "agent", context=context, run_config=run_config)
    if agent is None and (model is None or isinstance(model, str)):
        _model(model)
    own = agent
    obj = AgentObject(name, **options)

    async def answer(ctx: SessionContext, text: str) -> Any:
        run = _Options(await configured(context, ctx), max_turns, run_config)
        return await _prompt(
            ctx,
            text,
            own,
            await configured(model, ctx),
            await configured(system, ctx),
            await configured(tools, ctx),
            output,
            run,
        )

    obj.chat(answer, items=ITEMS, output=output, summarize=_summarizer(model) if own is None else None)
    return obj


def _summarizer(model: PerSession[AnyModel | None]) -> Summarize:
    """Return how the built-in agent summarizes its earlier turns: with its own model, in a run of its own."""

    async def summarize(ctx: SessionContext, request: str) -> str:
        chosen = _model(await configured(model, ctx))
        result = await Runner.run(Agent(name="summarizer"), request, run_config=RunConfig(model=chosen))
        return str(result.final_output)

    return summarize
