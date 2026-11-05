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
- A reply that cannot be resumed that way (a handoff, say) is dropped, and its
  agent is asked again. A call to a tool the agent does not have (which the
  SDK fails the run for) is reported back to the model to fix, on the retry.
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
"""

from __future__ import annotations

import dataclasses
import json
import typing
from collections.abc import Awaitable, Sequence
from typing import Any, Unpack

from agents import (
    Agent,
    FunctionTool,
    Handoff,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    Model,
    ModelResponse,
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
from pydantic_core import to_json

from restate.agents._common import PerSession, check_own, configured, from_tool, model_id_of, model_of
from restate.agents._conversation import Call, Conversation, Item, Parsed, Result, Run, answer_once
from restate.agents._object import AgentObject, AgentOptions
from restate.agents._tools import Tool, current_call, tools_in
from restate.agents._turn import tool_call_ended, tool_call_started
from restate.exceptions import TerminalError
from restate.sessions import SessionContext

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

    def reply(self, text: str) -> Item:
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
    """Return the agent the conversation is with: the latest reply's, or the agent that reply handed off to."""
    latest = next((e for e in reversed(conversation.entries) if e.reply is not None), None)
    if latest is None or latest.reply is None:
        return root
    agent = everyone.get(latest.agent or "", root)
    targets = _handoffs(agent)
    for call in (call for e in conversation.entries if e.reply == latest.reply for call in e.calls):
        if call.name in targets and conversation.has_result(call.id):
            return everyone.get(targets[call.name], agent)
    return agent


# --- A run ----------------------------------------------------------------------------------------


class _Hooks(RunHooks[Any]):
    """Store each step of a run as it comes, and publish each tool call as it starts and ends."""

    def __init__(self, ctx: SessionContext, conversation: Conversation) -> None:
        self.ctx = ctx
        self.conversation = conversation

    async def model_input(self, data: CallModelData[Any]) -> ModelInputData:
        """The run's ``call_model_input_filter``: store the tools' outputs before the next request."""
        if outputs := self.conversation.new_results(data.model_data.input):
            await self.conversation.append(*outputs)
        return data.model_data

    async def on_llm_end(self, context: RunContextWrapper[Any], agent: Agent[Any], response: ModelResponse) -> None:
        reply = typing.cast(list[Item], response.to_input_items())
        await self.conversation.append_reply(*reply, agent=agent.name)

    async def on_tool_start(self, context: RunContextWrapper[Any], agent: Agent[Any], tool: SdkTool) -> None:
        await tool_call_started(self.ctx)

    async def on_tool_end(self, context: RunContextWrapper[Any], agent: Agent[Any], tool: SdkTool, result: Any) -> None:
        await tool_call_ended(self.ctx)


async def _resume(
    ctx: SessionContext, conversation: Conversation, agent: Agent[Any], everyone: dict[str, Agent[Any]]
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
    found = {tool.name: tool for tool in await agent.get_all_tools(RunContextWrapper(None))}
    known = {*found, *_handoffs(agent)}
    if any(call.name in known and not isinstance(found.get(call.name), FunctionTool) for call in calls):
        # A call that only the SDK can make (a handoff, a hosted tool): ask the agent again instead.
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
            given = ToolContext(None, tool_name=tool.name, tool_call_id=call.id, tool_arguments=arguments)
            await tool_call_started(ctx)
            output = _output(await _unwrapped(ctx, tool.on_invoke_tool(given, arguments)))
            await tool_call_ended(ctx)
        outputs.extend(ITEMS.results([(call, output)]))
    await conversation.append(*outputs)
    return agent


async def _unwrapped(ctx: SessionContext, work: Awaitable[Any]) -> Any:
    """Await ``work``, raising a tool's error as the tool raised it rather than as the SDK wraps it.

    Too many turns, which a retry would only repeat, become terminal.
    """
    try:
        return await work
    except UserError as error:
        cause = error.__cause__
        if isinstance(cause, Exception):
            # Mark the error as the tool's, so that it is never mistaken for the model provider refusing.
            from_tool(ctx, cause)
            raise cause from None
        raise
    except MaxTurnsExceeded as error:
        raise TerminalError(str(error)) from error


def _run(
    ctx: SessionContext, own: Agent[Any] | None, model: AnyModel | None, system: str | None, given: Sequence[AnyTool]
) -> Run:
    """Return the run of a prompt: the ``Runner``, from the conversation to the prompt's answer."""
    if own is None:
        root: Agent[Any] = Agent(name="assistant", instructions=system, tools=tools(given))
        config = RunConfig(model=_model(model), nest_handoff_history=False)
    else:
        root = own.clone(tools=[*own.tools, *tools(given)]) if given else own
        # A model given overrides each agent's own, as RunConfig does.
        config = RunConfig(model=_model(model) if model is not None else None, nest_handoff_history=False)
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
            return await _go(ctx, conversation, _current(conversation, root, everyone), everyone, config)

    return run


async def _go(
    ctx: SessionContext,
    conversation: Conversation,
    current: Agent[Any],
    everyone: dict[str, Agent[Any]],
    config: RunConfig,
) -> str:
    """Run the ``Runner`` on from the conversation, with the agent it is with, and return the answer."""
    agent = await _unwrapped(ctx, _resume(ctx, conversation, current, everyone))
    hooks = _Hooks(ctx, conversation)
    filtered = dataclasses.replace(config, call_model_input_filter=hooks.model_input)
    items = typing.cast(Any, [entry.item for entry in conversation.entries])
    try:
        result = await _unwrapped(ctx, Runner.run(agent, items, hooks=hooks, run_config=filtered))
    except (InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered) as error:
        # The reply the guardrail blocked is not kept.
        if (blocked := conversation.last_reply()) is not None:
            await conversation.drop(blocked)
        raise TerminalError(str(error)) from error
    # The outputs that no model request stored: those of a tool that ends the turn, say.
    if outputs := conversation.new_results(result.to_input_list()):
        await conversation.append(*outputs)
    return _output(result.final_output)


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


def _model(model: AnyModel | None) -> str | Model:
    """Return a model of the SDK, or the model id of ``openai/<model-id>``; raise ``ModelError`` for another provider."""
    return model if isinstance(model, Model) else model_id_of(model_of(model), "openai")


# --- The API --------------------------------------------------------------------------------------


async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    agent: Agent[Any] | None = None,
    model: AnyModel | None = None,
    system: str | None = None,
    tools: Sequence[AnyTool] = (),
) -> str:
    """Answer a prompt with an OpenAI Agents SDK agent, exactly once.

    For a handler of your own; the conversation is the object's key, shared
    with its ``chat``. A retried attempt resumes the same prompt rather than
    asking again, and each prompt of an invocation is answered on its own::

        @assistant.handler
        async def summarize(ctx: sessions.SessionContext, topic: str) -> str:
            return await openai.prompt(ctx, f"Summarize what we said about {topic}")

    Args:
        ctx: The handler's context.
        text: The prompt.
        agent: An ``Agent`` of your own (with handoffs, guardrails, ...), whose
            output is text. Defaults to one with ``system`` as its instructions.
        model: ``"openai/<model-id>"``, or a model of the SDK, for every agent
            of the run. Defaults to the models of ``agent`` and the agents it
            hands off to; without ``agent``, to ``$RESTATE_AGENT_MODEL``, else
            ``"openai/gpt-6-sol"``.
        system: The instructions, when there is no ``agent``.
        tools: ``restate.agents`` tools, or tools of the SDK, beside
            ``agent``'s own. For the agents it hands off to, see ``tools()``.

    Returns:
        The agent's answer.

    Raises:
        TypeError: If both ``agent`` and ``system`` are given.
    """
    check_own(agent, system)

    return await answer_once(ctx, text, ITEMS, _run(ctx, agent, model, system, tools))


def agent(
    name: str,
    openai_agent: Agent[Any] | None = None,
    /,
    *,
    model: PerSession[AnyModel | None] = None,
    system: PerSession[str | None] = None,
    tools: PerSession[Sequence[AnyTool]] = (),
    **options: Unpack[AgentOptions],
) -> AgentObject:
    """Create an OpenAI Agents SDK agent: a virtual object with one conversation per key.

    Its ``chat`` handler answers each message with the SDK's ``Runner``; the
    conversation is kept in the key's database, a step at a time. Add
    handlers of your own with ``@assistant.handler``::

        assistant = openai.agent("assistant", system="Answer briefly.", tools=[get_weather])

    Or bring an ``Agent`` of your own, with handoffs and guardrails::

        assistant = openai.agent("desk", Agent(name="triage", handoffs=[refunds]))

    ``model``, ``system`` and ``tools`` may each be a function of the
    invocation's context, sync or async, for values that differ per user::

        assistant = openai.agent("assistant", system=lambda ctx: f"You help {ctx.key}.")

    Args:
        name: The object's name, as Restate serves it.
        openai_agent: An ``Agent`` of your own, whose output is text.
            Defaults to one with ``system`` as its instructions.
        model: ``"openai/<model-id>"``, or a model of the SDK, for every agent
            of a run. Defaults to the models of ``openai_agent`` and the agents
            it hands off to; without ``openai_agent``, to
            ``$RESTATE_AGENT_MODEL``, else ``"openai/gpt-6-sol"``.
        system: The instructions, when there is no ``openai_agent``.
        tools: ``restate.agents`` tools, or tools of the SDK.
        **options: The object's options, such as ``description``,
            ``background`` or ``invocation_retry_policy``: see ``AgentOptions``.

    Returns:
        The agent, to serve with ``restate.serve(restate.app([agent]))``, or any ASGI server.

    Raises:
        TypeError: If both ``openai_agent`` and ``system`` are given.
        TerminalError: If ``model`` names a provider other than OpenAI.
    """
    check_own(openai_agent, system)
    if isinstance(model, str) or (model is None and openai_agent is None):
        _model(model)
    obj = AgentObject(name, **options)

    async def answer(ctx: SessionContext, text: str) -> str:
        return await prompt(
            ctx,
            text,
            agent=openai_agent,
            model=await configured(model, ctx),
            system=await configured(system, ctx),
            tools=await configured(tools, ctx),
        )

    obj.chat(answer, items=ITEMS)
    return obj
