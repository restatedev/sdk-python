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

The SDK's ``Runner`` runs the agent. The handler is not replayed: a retried
attempt (after a crash or a lost connection) runs again from the start, and
the run resumes from the last step stored:

- The conversation is stored before each model request and with each model
  response (``call_model_input_filter`` and ``RunHooks.on_llm_end``), in order
  with everything else the handler does. The tool calls a model made are
  therefore stored before any tool runs, and a crash while tools are running
  runs them again with the same call ids, which a tool can pass on to stay
  idempotent. (The SDK's own ``Session`` stores a step only once its tools
  have run.)
- A reply that cannot be resumed that way (a handoff, say) is dropped, and its
  agent is asked again. A call to a tool the agent does not have (which the
  SDK fails the run for) is reported back to the model to fix, on the retry.
- The agent a handoff gave the conversation to answers the next messages too.
  The stored conversation is the full transcript: a handoff passes it on as
  it is, rather than collapsed into one message (``nest_handoff_history`` is
  off). A handoff's own ``input_filter`` changes what is stored as well.
- A tool's error fails the attempt, and Restate retries it. A
  ``restate.TerminalError``, which a retry would only repeat, is instead the
  tool's answer to the model.
- A guardrail's tripwire fails the prompt for good, and the reply it blocked
  is dropped from the conversation. A stored reply becomes the answer
  without asking again only if its agent has no output guardrails.
- Each message is answered once per invocation: its answer is stored under the
  prompt's id, and a retried attempt returns it.
"""

from __future__ import annotations

import collections
import contextvars
import dataclasses
import json
import typing
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

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
from agents import (
    Tool as SdkTool,
)
from agents.run import CallModelData, ModelInputData
from agents.tool_context import ToolContext
from pydantic_core import to_json

from restate.agents._calls import outcomes
from restate.agents._common import (
    CANCELLED,
    UNANSWERED,
    PerSession,
    configured,
    cut_off_result,
    from_tool,
    model_id_of,
    model_of,
    next_prompt_id,
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

AnyModel: typing.TypeAlias = str | Model
"""A model: ``"openai/<model-id>"`` (or the model id alone), or a model of the SDK."""

AnyTool: typing.TypeAlias = Tool | SdkTool
"""A tool: one made with ``restate.agents`` (``tool``, ``call``, ``wait_for_signal``, ...), or a tool of the SDK."""

Item = dict[str, Any]
"""An item of the conversation, as the Responses API takes it as input."""

_SCHEMA = Schema(
    "restate_agents.openai",
    [
        """CREATE TABLE restate_openai_items (
            n INTEGER PRIMARY KEY,
            item TEXT NOT NULL,
            tag TEXT
        )""",
        "CREATE TABLE restate_openai_answers (prompt TEXT PRIMARY KEY, answer TEXT NOT NULL)",
        "CREATE TABLE restate_openai_agent (row INTEGER PRIMARY KEY CHECK (row = 0), name TEXT NOT NULL)",
    ],
)
"""The conversation's items, each tagged with the prompt (``prompt:<id>``) or the model's response
(``response:<id>:<agent>``) it is part of; each prompt's answer; and the agent the conversation is with."""

_SESSION: contextvars.ContextVar[SessionContext] = contextvars.ContextVar("restate_agents_openai_session")
"""The context of the run in progress, for the ``restate.agents`` tools that it calls."""


# --- Storage --------------------------------------------------------------------------------------


@dataclass
class _Stored:
    """The conversation as stored: its items, and the tag of each tagged item, by position."""

    items: list[Item]
    tags: dict[int, str]

    def tagged(self, tag: str) -> list[int]:
        return [n for n, t in self.tags.items() if t == tag]

    def replaced(self, items: list[Item]) -> _Stored:
        """Return the conversation as ``items``, a new version of it, with each item keeping its tag.

        An item keeps the tag of the stored item it equals, in order, wherever
        it is now: a handoff's ``input_filter`` may have dropped items before it.
        """
        stored: dict[str, collections.deque[str | None]] = {}
        for n, item in enumerate(self.items):
            stored.setdefault(_dumped(item), collections.deque()).append(self.tags.get(n))
        tags: dict[int, str] = {}
        for n, item in enumerate(items):
            same = stored.get(_dumped(item))
            tag = same.popleft() if same else None
            if tag is not None:
                tags[n] = tag
        return _Stored(items, tags)

    def without(self, tag: str) -> _Stored:
        """Return the conversation without the items tagged ``tag`` (a reply)."""
        kept = [n for n in range(len(self.items)) if self.tags.get(n) != tag]
        return _Stored([self.items[n] for n in kept], {i: self.tags[n] for i, n in enumerate(kept) if n in self.tags})

    def last_reply(self) -> str | None:
        """Return the tag of the model's reply that the conversation ends with, if it ends with one."""
        tag = self.tags.get(len(self.items) - 1)
        return tag if tag is not None and tag.startswith("response:") else None

    def prompts(self) -> list[str]:
        """Return the ids of the prompts in the conversation, in order."""
        return [t.removeprefix("prompt:") for _, t in sorted(self.tags.items()) if t.startswith("prompt:")]


class _Conversation:
    """The conversation in the key's database.

    ``save`` writes the items that changed since the last load or save, in one
    transaction.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self._saved: list[str] = []
        self._tags: dict[int, str] = {}

    async def load(self) -> _Stored:
        await _SCHEMA.ensure(self.db)
        rows = await self.db.values("SELECT item, tag FROM restate_openai_items ORDER BY n")
        self._saved = [str(item) for item, _ in rows]
        self._tags = {n: str(tag) for n, (_, tag) in enumerate(rows) if tag is not None}
        return _Stored([json.loads(item) for item in self._saved], dict(self._tags))

    async def save(self, stored: _Stored) -> None:
        dumped = [json.dumps(item) for item in stored.items]
        first = next(
            (
                n
                for n, (now, saved) in enumerate(zip(dumped, self._saved, strict=False))
                if now != saved or stored.tags.get(n) != self._tags.get(n)
            ),
            min(len(dumped), len(self._saved)),
        )
        if first == len(dumped) == len(self._saved):
            return
        async with self.db.transaction() as tx:
            await tx.execute("DELETE FROM restate_openai_items WHERE n >= ?", [first])
            for n in range(first, len(dumped)):
                await tx.execute(
                    "INSERT INTO restate_openai_items (n, item, tag) VALUES (?, ?, ?)",
                    [n, dumped[n], stored.tags.get(n)],
                )
        self._saved = dumped
        self._tags = dict(stored.tags)

    async def answer_of(self, prompt_id: str) -> str | None:
        await _SCHEMA.ensure(self.db)
        row = await self.db.get("SELECT answer FROM restate_openai_answers WHERE prompt = ?", [prompt_id])
        return None if row is None else str(row["answer"])

    async def answer(self, prompt_id: str, text: str) -> None:
        await self.db.execute("INSERT INTO restate_openai_answers (prompt, answer) VALUES (?, ?)", [prompt_id, text])

    async def agent(self) -> str | None:
        """Return the name of the agent the conversation is with, if a handoff gave it to one."""
        await _SCHEMA.ensure(self.db)
        row = await self.db.get("SELECT name FROM restate_openai_agent")
        return None if row is None else str(row["name"])

    async def hand_to(self, name: str) -> None:
        await self.db.execute("INSERT OR REPLACE INTO restate_openai_agent (row, name) VALUES (0, ?)", [name])


# --- The conversation's shape ---------------------------------------------------------------------


def _dumped(item: Item) -> str:
    return json.dumps(item, sort_keys=True)


def _agent_of(reply: str) -> str:
    """Return the name of the agent whose reply is tagged ``reply`` (``response:<id>:<agent>``)."""
    return reply.split(":", 2)[-1]


def _unanswered(items: list[Item]) -> list[Item]:
    """Return the function calls of ``items`` that have no output."""
    answered = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    return [item for item in items if item.get("type") == "function_call" and item.get("call_id") not in answered]


def _text(item: Item) -> str:
    """Return the text of a message item."""
    content = item.get("content")
    if isinstance(content, str):
        return content
    parts = typing.cast(list[dict[str, Any]], content or [])
    return "".join(str(part.get("text", "")) for part in parts)


def _final_reply(stored: _Stored) -> str | None:
    """Return the text of the conversation's last reply if it is a final one: a message, and no function calls."""
    tag = stored.last_reply()
    if tag is None:
        return None
    reply = [stored.items[n] for n in stored.tagged(tag)]
    messages = [item for item in reply if item.get("type", "message") == "message" and item.get("role") == "assistant"]
    if not messages or any(item.get("type") == "function_call" for item in reply):
        return None
    return _text(messages[-1])


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


# --- A run ----------------------------------------------------------------------------------------


class _Run(RunHooks[Any]):
    """A run of the ``Runner`` on the stored conversation, which it stores a step at a time.

    It stores the model's input before each request (``model_input``, the
    run's ``call_model_input_filter``) and each response as it arrives
    (``on_llm_end``), and publishes each tool call as it starts and ends.
    """

    def __init__(self, ctx: SessionContext, conversation: _Conversation, stored: _Stored, agent: str) -> None:
        self.ctx = ctx
        self.conversation = conversation
        self.stored = stored
        self.agent = agent
        """The name of the agent the conversation is with."""

    async def model_input(self, data: CallModelData[Any]) -> ModelInputData:
        self.stored = self.stored.replaced(typing.cast(list[Item], list(data.model_data.input)))
        await self.conversation.save(self.stored)
        return data.model_data

    async def on_llm_end(self, context: RunContextWrapper[Any], agent: Agent[Any], response: ModelResponse) -> None:
        reply = list(response.to_input_items())
        tag = f"response:{uuid.uuid4().hex}:{agent.name}"
        start = len(self.stored.items)
        tags = {**self.stored.tags, **dict.fromkeys(range(start, start + len(reply)), tag)}
        self.stored = _Stored([*self.stored.items, *typing.cast(list[Item], reply)], tags)
        await self.conversation.save(self.stored)

    async def on_agent_start(self, context: RunContextWrapper[Any], agent: Agent[Any]) -> None:
        if agent.name != self.agent:
            self.agent = agent.name
            await self.conversation.hand_to(agent.name)

    async def on_tool_start(self, context: RunContextWrapper[Any], agent: Agent[Any], tool: SdkTool) -> None:
        await tool_call_started(self.ctx)

    async def on_tool_end(self, context: RunContextWrapper[Any], agent: Agent[Any], tool: SdkTool, result: Any) -> None:
        await tool_call_ended(self.ctx)

    async def go(self, root: Agent[Any], everyone: dict[str, Agent[Any]], config: RunConfig) -> Any:
        """Run on from the stored conversation, with the agent it is with, and return the ``Runner``'s result."""
        agent = await _unwrapped(self.ctx, self.resume(everyone.get(self.agent, root), everyone))
        filtered = dataclasses.replace(config, call_model_input_filter=self.model_input)
        result = await _unwrapped(self.ctx, self._guarded(agent, filtered))
        # The items the run ended with, which no model request stored: those of a tool that ends the turn, say.
        self.stored = self.stored.replaced(typing.cast(list[Item], result.to_input_list()))
        await self.conversation.save(self.stored)
        return result

    async def _guarded(self, agent: Agent[Any], config: RunConfig) -> Any:
        """Run the ``Runner``; a reply that a guardrail's tripwire blocked is dropped, and the prompt fails for good."""
        try:
            return await Runner.run(agent, typing.cast(Any, self.stored.items), hooks=self, run_config=config)
        except (InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered) as error:
            await self.drop_last_reply()
            raise TerminalError(str(error)) from error

    async def drop_last_reply(self) -> None:
        """Drop the model's reply that the conversation ends with, if it ends with one."""
        tag = self.stored.last_reply()
        if tag is not None:
            self.stored = self.stored.without(tag)
            await self.conversation.save(self.stored)

    async def resume(self, agent: Agent[Any], everyone: dict[str, Agent[Any]]) -> Agent[Any]:
        """Resume from a reply whose tool calls have no outputs (cut off by a crash), and return the agent to run.

        Function tools of ``agent`` run again with their call ids, and a call to
        a tool that ``agent`` does not have gets an error for the model to act
        on. A reply with any other call (a handoff, say) is dropped instead, and
        the agent that gave it is asked again.
        """
        calls = _unanswered(self.stored.items)
        if not calls:
            return agent
        # The agent whose reply it is, which a handoff may since have passed the conversation on from.
        cut = self.stored.tags.get(self.stored.items.index(calls[0]), "")
        agent = everyone.get(_agent_of(cut), agent)
        found = {tool.name: tool for tool in await agent.get_all_tools(RunContextWrapper(None))}
        known = {*found, *(_handoff_name(h) for h in agent.handoffs)}
        if all(
            isinstance(found.get(str(call.get("name"))), FunctionTool) or call.get("name") not in known
            for call in calls
        ):
            outputs = [await self._output_of(call, found.get(str(call.get("name")))) for call in calls]
            self.stored = _Stored([*self.stored.items, *outputs], self.stored.tags)
        else:
            self.stored = self.stored.without(cut)
        await self.conversation.save(self.stored)
        return agent

    async def _output_of(self, call: Item, tool: SdkTool | None) -> Item:
        """Run a function call again with its id, or tell the model that its tool does not exist."""
        if not isinstance(tool, FunctionTool):
            output = f"Error: there is no tool named {call.get('name')!r}"
        else:
            arguments = str(call.get("arguments") or "{}")
            given = ToolContext(None, tool_name=tool.name, tool_call_id=str(call["call_id"]), tool_arguments=arguments)
            await tool_call_started(self.ctx)
            output = _output(await _unwrapped(self.ctx, tool.on_invoke_tool(given, arguments)))
            await tool_call_ended(self.ctx)
        return {"type": "function_call_output", "call_id": call["call_id"], "output": output}


def _handoff_name(target: Agent[Any] | Handoff[Any, Any]) -> str:
    return target.tool_name if isinstance(target, Handoff) else Handoff.default_tool_name(target)


def _output(value: Any) -> str:
    return value if isinstance(value, str) else to_json(value).decode()


async def _unwrapped(ctx: SessionContext, work: typing.Awaitable[Any]) -> Any:
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
        return await tool.run(args, ToolCall(_SESSION.get(), context.tool_call_id))

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


async def _close(ctx: SessionContext, conversation: _Conversation, prompt_id: str, text: str) -> None:
    """Close the cancelled, refused or failed run of prompt ``prompt_id`` with ``text``.

    Each function call without an output gets one: the error its Restate call
    returned, a note that it did happen if it returned, or else ``text``. Then
    ``text`` becomes the reply and the prompt's answer. A run that stored no
    prompt yet has nothing to close.
    """
    stored = await conversation.load()
    if prompt_id not in stored.prompts() or await conversation.answer_of(prompt_id) is not None:
        return
    calls = _unanswered(stored.items)
    returned = await outcomes(ctx, [str(call["call_id"]) for call in calls])
    closing: list[Item] = [
        *(
            {
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": cut_off_result(returned, str(call["call_id"]), text),
            }
            for call in calls
        ),
        {"role": "assistant", "content": text},
    ]
    await conversation.save(_Stored([*stored.items, *closing], stored.tags))
    await conversation.answer(prompt_id, text)


async def _answer_once(
    ctx: SessionContext,
    text: str,
    own: Agent[Any] | None,
    model: AnyModel | None,
    system: str | None,
    given: Sequence[AnyTool],
) -> str:
    if own is None:
        root: Agent[Any] = Agent(name="assistant", instructions=system, tools=tools(given))
        config = RunConfig(model=_model(model), nest_handoff_history=False)
    else:
        root = own.clone(tools=[*own.tools, *tools(given)]) if given else own
        # A model given overrides each agent's own, as RunConfig does.
        config = RunConfig(model=_model(model) if model is not None else None, nest_handoff_history=False)
    everyone = _agents(root)
    conversation = _Conversation(ctx.db)
    prompt_id = next_prompt_id(ctx)
    while True:
        if (answer := await conversation.answer_of(prompt_id)) is not None:
            return stored_answer(answer)
        stored = await conversation.load()
        # Finish an interrupted run first (this prompt's own, after a crash), then start this prompt's run.
        asked = stored.prompts()
        running = asked[-1] if asked and await conversation.answer_of(asked[-1]) is None else None
        if running is not None and (final := _final_reply(stored)) is not None:
            reply = stored.last_reply() or ""
            replier = everyone.get(_agent_of(reply), root)
            if not (replier.output_guardrails or config.output_guardrails):
                # The run had stored its final reply, but not its answer: store it, rather than ask the model again.
                await conversation.answer(running, final)
                continue
            # Its output guardrails may not have passed it: ask again, and the guardrails run on the new reply.
            stored = stored.without(reply)
            await conversation.save(stored)
        if running is None:
            running = prompt_id
            stored.tags[len(stored.items)] = f"prompt:{prompt_id}"
            stored.items.append({"role": "user", "content": text})
            await conversation.save(stored)
        run = _Run(ctx, conversation, stored, await conversation.agent() or root.name)
        stop = stop_signal_of(ctx)
        session = _SESSION.set(ctx)
        try:
            going = run.go(root, everyone, config)
            result = await (stop.guard(going) if stop is not None else going)
        except AttemptEnded:
            # The attempt ended: stop here, and let the next attempt continue the run.
            raise
        except TerminalError as error:
            if error.status_code == _CANCELLATION:
                # Cancelled: close the run, so that the next prompt starts afresh.
                await _close(ctx, conversation, running, CANCELLED)
                raise
            # Failed for good: close the run with the error, so that the next prompt does not run into it again.
            await _close(ctx, conversation, running, f"Error: {error.message}")
            if running == prompt_id:
                raise
            continue
        except Exception as error:
            # Refused for good (a wrong API key, say): close the run unanswered and fail with a terminal error.
            # If the run was an earlier prompt's, carry on with this one.
            refused = refusal_of(ctx, error)
            if refused is None:
                raise
            await _close(ctx, conversation, running, UNANSWERED)
            if running == prompt_id:
                raise refused from error
            continue
        finally:
            _SESSION.reset(session)
        await conversation.answer(running, _output(result.final_output))


async def _transcript(ctx: SessionContext) -> Transcript:
    """Return the conversation in the form ``messages`` returns."""
    transcript = Transcript()
    for item in (await _Conversation(ctx.db).load()).items:
        kind = item.get("type", "message")
        if kind == "message" and item.get("role") in ("user", "assistant"):
            transcript.say("user" if item["role"] == "user" else "assistant", _text(item))
        elif kind == "function_call":
            arguments = str(item.get("arguments") or "{}")
            try:
                input: Any = json.loads(arguments)
            except ValueError:
                input = arguments
            transcript.call(str(item["call_id"]), str(item["name"]), input)
        elif kind == "function_call_output":
            output = item.get("output")
            transcript.result(str(item["call_id"]), output if isinstance(output, str) else json.dumps(output))
    return transcript


# --- The API --------------------------------------------------------------------------------------


def _check(own: Agent[Any] | None, system: object) -> None:
    if own is not None and system is not None:
        raise TypeError("an Agent of your own has its own instructions: pass no system")


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
    _check(agent, system)

    async def read() -> Transcript:
        return await _transcript(ctx)

    async def work() -> str:
        return await _answer_once(ctx, text, agent, model, system, tools)

    return await transcribing(ctx, read, work)


def agent(
    name: str,
    openai_agent: Agent[Any] | None = None,
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
        TypeError: If both ``openai_agent`` and ``system`` are given.
        TerminalError: If ``model`` names a provider other than OpenAI.
    """
    _check(openai_agent, system)
    if isinstance(model, str) or (model is None and openai_agent is None):
        _model(model)
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
            agent=openai_agent,
            model=await configured(model, ctx),
            system=await configured(system, ctx),
            tools=await configured(tools, ctx),
        )

    obj.chat(answer)
    return obj
