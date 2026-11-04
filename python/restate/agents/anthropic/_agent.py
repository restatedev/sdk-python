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
"""Claude agents on the Anthropic API, each conversation in the object's database.

The agent runs the tool-calling loop itself, one step at a time, and stores
each step before the next:

- The model's reply is stored before its tools run, and their results before
  the next request. So a crash during the tools runs them again with the same
  tool call ids, which a tool passes on to stay idempotent, without asking the
  model again.
- A tool's error fails the attempt, which Restate retries; a
  ``restate.TerminalError`` is the tool's answer to the model.
- A message is answered once per invocation: the reply that ends its run (one
  that calls no tools) is its answer, and a retried attempt returns it.

The conversation is only ever appended to, as the API requires for the model's
thinking blocks, which are sent back unchanged. Requests ask the API to drop
thinking blocks it can no longer use (``block_binding``) rather than refuse
the request, so a conversation survives a release that changes the agent's
system prompt or tools.
"""

from __future__ import annotations

import asyncio
import functools
import json
import typing
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

import anthropic
from restate.agents._calls import outcomes
from restate.agents._common import (
    CANCELLED,
    CLAUDE_MODEL,
    UNANSWERED,
    PerSession,
    configured,
    cut_off_result,
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

Effort = Literal["low", "medium", "high", "xhigh", "max"]
"""How much effort the model puts into each reply."""

AnyTool: typing.TypeAlias = Tool | dict[str, Any]
"""A tool: one of ``restate.agents`` (``tool``, ``call``, ``wait_for_signal``...), or a tool definition of the
Anthropic API, such as a server tool (``{"type": "web_search_20260209", "name": "web_search"}``)."""

_CANCELLATION = 409
"""The status of Restate's cancellation, as a turn that was stopped fails with it."""

_BINDING_BETA = "thinking-binding-controls-2026-08-01"
"""Lets a request drop the thinking blocks the API can no longer use, instead of failing."""

_FALLBACK_BETA = "server-side-fallback-2026-07-01"
"""Lets the API answer a declined request with a fallback model (``fallbacks="default"``)."""

_GOES_ON = frozenset({"tool_use", "pause_turn"})
"""The stop reasons of a reply that does not end its run: its tools run, or it is sent back to go on.

Any other ends the run, including ``closed``, which marks a run this package closed (see ``_close``).
"""

_CUT_OFF = {
    "max_tokens": "the model's reply was cut off at max_tokens={max_tokens}: raise max_tokens",
    "model_context_window_exceeded": "the conversation no longer fits the model's context window",
}
"""The stop reasons of a reply that fails its prompt for good, with the error's message."""

_DECLINED = "The model declined to answer."

_SCHEMA = Schema(
    "restate_agents.anthropic",
    [
        """CREATE TABLE restate_anthropic_messages (
            n INTEGER PRIMARY KEY,
            message TEXT NOT NULL,
            prompt TEXT,
            stop_reason TEXT
        )"""
    ],
)


@dataclass
class _Stored:
    """A message of the conversation: as the API takes it, the prompt it is (if one), and why the model stopped."""

    message: dict[str, Any]
    prompt: str | None = None
    stop: str | None = None

    @property
    def role(self) -> str:
        return str(self.message["role"])

    @property
    def goes_on(self) -> bool:
        """Whether it is a reply that does not end its run."""
        return self.role == "assistant" and self.stop in _GOES_ON

    @property
    def blocks(self) -> list[dict[str, Any]]:
        content = self.message["content"]
        return [{"type": "text", "text": content}] if isinstance(content, str) else list(content)


class _Conversation:
    """The conversation in the key's database, one message per row, only ever appended to."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def load(self) -> list[_Stored]:
        await _SCHEMA.ensure(self.db)
        rows = await self.db.values("SELECT message, prompt, stop_reason FROM restate_anthropic_messages ORDER BY n")
        return [_Stored(json.loads(str(message)), _str(prompt), _str(stop)) for message, prompt, stop in rows]

    async def append(self, history: list[_Stored], *added: _Stored) -> list[_Stored]:
        """Append ``added`` after ``history`` (the conversation as loaded), in one transaction; return the result."""
        async with self.db.transaction() as tx:
            for n, stored in enumerate(added, start=len(history)):
                await tx.execute(
                    "INSERT INTO restate_anthropic_messages (n, message, prompt, stop_reason) VALUES (?, ?, ?, ?)",
                    [n, json.dumps(stored.message), stored.prompt, stored.stop],
                )
        return [*history, *added]


def _str(value: object) -> str | None:
    return None if value is None else str(value)


# --- The conversation's shape ---------------------------------------------------------------------


def _after_fallback(blocks: list[dict[str, Any]]) -> int:
    """Return the index after a reply's last ``fallback`` block (where a fallback model took over), or 0."""
    marks = [i for i, block in enumerate(blocks) if block.get("type") == "fallback"]
    return marks[-1] + 1 if marks else 0


def _tool_uses(stored: _Stored) -> list[dict[str, Any]]:
    """Return the client tool calls of a reply: those made after any fallback, by the model that answered."""
    blocks = stored.blocks
    return [block for block in blocks[_after_fallback(blocks) :] if block.get("type") == "tool_use"]


def _replay(stored: _Stored) -> dict[str, Any]:
    """Return a message as the API takes it back.

    A reply that a fallback model took over mid-output keeps, from before the
    last ``fallback`` block, only its text and its paired server tool blocks,
    as the API requires; the ``fallback`` blocks stay where they are, and
    everything after the last one is sent back unchanged.
    """
    blocks = stored.blocks
    boundary = _after_fallback(blocks)
    if stored.role != "assistant" or boundary == 0:
        return stored.message
    before = blocks[:boundary]
    results = {block.get("tool_use_id") for block in before if str(block.get("type")).endswith("_tool_result")}

    def kept(block: dict[str, Any]) -> bool:
        kind = str(block.get("type"))
        if kind in ("server_tool_use", "mcp_tool_use"):
            return block.get("id") in results
        return kind in ("text", "fallback") or kind.endswith("_tool_result")

    return {"role": "assistant", "content": [*filter(kept, before), *blocks[boundary:]]}


def _text(stored: _Stored) -> str:
    return "".join(str(block.get("text", "")) for block in stored.blocks if block.get("type") == "text")


def _answer_after(history: list[_Stored], asked: int) -> str | None:
    """Return the answer to the prompt at ``asked``, once its run has ended.

    The answer is the text of the replies that end the run, after its last
    tool results: one reply, or more where the API paused a long turn
    (``pause_turn``). A run this package closed has its closing text alone.
    """
    replies: list[_Stored] = []
    for stored in history[asked + 1 :]:
        if stored.prompt is not None:
            return None
        if stored.role == "user":
            replies = []
        elif stored.stop == "closed":
            return _text(stored)
        else:
            replies.append(stored)
            if not stored.goes_on:
                return "".join(_text(reply) for reply in replies)
    return None


def _running(history: list[_Stored]) -> str | None:
    """Return the id of the prompt whose run the conversation ends with, if that run has not ended."""
    asked = next((i for i in reversed(range(len(history))) if history[i].prompt is not None), None)
    return None if asked is None or _answer_after(history, asked) is not None else history[asked].prompt


# --- The loop -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Options:
    model: str | None
    system: str | None
    tools: Sequence[AnyTool]
    client: anthropic.AsyncAnthropic | None
    max_tokens: int
    effort: Effort | None
    fallbacks: bool


@functools.cache
def _default_client() -> anthropic.AsyncAnthropic:
    """Return the process's client, with credentials from the environment; created on first use."""
    return anthropic.AsyncAnthropic()


def _definition(tool: AnyTool) -> dict[str, Any]:
    if isinstance(tool, Tool):
        return {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
    return tool


async def _reply(history: list[_Stored], options: _Options) -> _Stored:
    """Ask the model for the next reply to ``history``."""
    params: dict[str, Any] = {
        "model": model_id_of(model_of(options.model, CLAUDE_MODEL), "anthropic"),
        "max_tokens": options.max_tokens,
        "messages": [_replay(stored) for stored in history],
        "thinking": {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
        # The conversation grows by appending: caching its prefix saves most of each request.
        "cache_control": {"type": "ephemeral"},
        "betas": [_BINDING_BETA, *([_FALLBACK_BETA] if options.fallbacks else [])],
    }
    if options.system is not None:
        params["system"] = options.system
    if options.tools:
        params["tools"] = [_definition(tool) for tool in options.tools]
    if options.effort is not None:
        params["output_config"] = {"effort": options.effort}
    if options.fallbacks:
        params["fallbacks"] = "default"
    client = options.client or _default_client()
    async with client.beta.messages.stream(**params) as stream:
        message = await stream.get_final_message()
    if message.stop_reason in _CUT_OFF:
        raise TerminalError(_CUT_OFF[message.stop_reason].format(max_tokens=options.max_tokens))
    content = [block.to_dict(mode="json", exclude_none=True) for block in message.content]
    if message.stop_reason == "refusal" and not any(block.get("type") == "text" for block in content):
        content.append({"type": "text", "text": _DECLINED})
    return _Stored({"role": "assistant", "content": content}, stop=message.stop_reason)


async def _run_tools(ctx: SessionContext, calls: list[dict[str, Any]], tools: Sequence[AnyTool]) -> _Stored:
    """Run the tool calls of a reply, at once, and return their results as one message."""
    named = {tool.name: tool for tool in tools if isinstance(tool, Tool)}

    async def result(call: dict[str, Any]) -> dict[str, Any]:
        tool = named.get(str(call.get("name")))
        if tool is None:
            text, failed = f"Error: there is no tool named {call.get('name')!r}", True
        else:
            text = await tool.run(call.get("input") or {}, ToolCall(ctx, str(call["id"])))
            failed = text.startswith("Error: ")
        return {"type": "tool_result", "tool_use_id": call["id"], "content": text, "is_error": failed}

    await tool_call_started(ctx)
    results = await asyncio.gather(*(result(call) for call in calls))
    await tool_call_ended(ctx)
    return _Stored({"role": "user", "content": list(results)})


async def _step(ctx: SessionContext, history: list[_Stored], options: _Options) -> _Stored:
    """Take the next step of the run that ``history`` ends with: run the last reply's tools, or ask the model.

    A reply that calls no tools and has not ended (``pause_turn``, where the
    API paused a long server tool loop) is sent back as is, to go on.
    """
    calls = _tool_uses(history[-1]) if history[-1].goes_on else []
    return await (_run_tools(ctx, calls, options.tools) if calls else _reply(history, options))


async def _close(ctx: SessionContext, conversation: _Conversation, text: str) -> None:
    """End the run that the conversation ends with, which was cancelled, refused or failed, with ``text``.

    Each tool call of its last reply that has no result gets one: the error
    its Restate call returned, a note that it did happen if it returned, or
    else ``text``. Then ``text`` ends the run as its answer.
    """
    history = await conversation.load()
    if _running(history) is None:
        return
    added: list[_Stored] = []
    calls = _tool_uses(history[-1]) if history[-1].goes_on else []
    if calls:
        returned = await outcomes(ctx, [str(call["id"]) for call in calls])
        results = [
            {
                "type": "tool_result",
                "tool_use_id": call["id"],
                "content": cut_off_result(returned, str(call["id"]), text),
                "is_error": True,
            }
            for call in calls
        ]
        added.append(_Stored({"role": "user", "content": results}))
    added.append(_Stored({"role": "assistant", "content": [{"type": "text", "text": text}]}, stop="closed"))
    await conversation.append(history, *added)


async def _answer_once(ctx: SessionContext, text: str, options: _Options) -> str:
    conversation = _Conversation(ctx.db)
    prompt_id = next_prompt_id(ctx)
    history = await conversation.load()
    while True:
        asked = next((i for i, stored in enumerate(history) if stored.prompt == prompt_id), None)
        if asked is not None and (answer := _answer_after(history, asked)) is not None:
            return stored_answer(answer)
        # Finish an interrupted run first (this prompt's own, after a crash), then start this prompt's run.
        running = _running(history)
        if running is None:
            history = await conversation.append(history, _Stored({"role": "user", "content": text}, prompt=prompt_id))
            continue
        stop = stop_signal_of(ctx)
        step = _step(ctx, history, options)
        try:
            added = await (stop.guard(step) if stop is not None else step)
        except AttemptEnded:
            # The attempt ended: it stops, and the next attempt goes on with the run.
            raise
        except TerminalError as error:
            if error.status_code == _CANCELLATION:
                # Cancelled: the run ends, and the next prompt starts afresh.
                await _close(ctx, conversation, CANCELLED)
                raise
            # Failed for good: the run ends with it, rather than the next prompt meeting it again.
            await _close(ctx, conversation, f"Error: {error.message}")
            if running == prompt_id:
                raise
            history = await conversation.load()
            continue
        except Exception as error:
            # Refused for good (a wrong API key): the run ends unanswered, and fails as terminal.
            # An earlier prompt's, this one goes on.
            refused = refusal_of(ctx, error)
            if refused is None:
                raise
            await _close(ctx, conversation, UNANSWERED)
            if running == prompt_id:
                raise refused from error
            history = await conversation.load()
            continue
        history = await conversation.append(history, added)


async def _transcript(ctx: SessionContext) -> Transcript:
    """Return the conversation, as ``messages`` shows it."""
    transcript = Transcript()
    for stored in await _Conversation(ctx.db).load():
        # As the model saw it: without the tool calls of a model that a fallback model took over from.
        seen = _Stored(_replay(stored))
        if any(block.get("type") == "text" for block in seen.blocks):
            transcript.say("assistant" if seen.role == "assistant" else "user", _text(seen))
        for block in seen.blocks:
            if block.get("type") == "tool_use":
                transcript.call(str(block["id"]), str(block["name"]), block.get("input"))
            elif block.get("type") == "tool_result":
                content = block.get("content")
                output = content if isinstance(content, str) else json.dumps(content)
                transcript.result(str(block["tool_use_id"]), output, bool(block.get("is_error")))
    return transcript


# --- The API --------------------------------------------------------------------------------------


async def prompt(
    ctx: SessionContext,
    text: str,
    *,
    model: str | None = None,
    system: str | None = None,
    tools: Sequence[AnyTool] = (),
    client: anthropic.AsyncAnthropic | None = None,
    max_tokens: int = 64_000,
    effort: Effort | None = None,
    fallbacks: bool = True,
) -> str:
    """Answer a prompt with Claude, exactly once.

    For a handler of your own; the conversation is the object's key, shared
    with its ``chat``. A retried attempt resumes the same prompt rather than
    asking again, and each prompt of an invocation is answered on its own::

        @assistant.handler
        async def summarize(ctx: sessions.SessionContext, topic: str) -> str:
            return await anthropic.prompt(ctx, f"Summarize what we said about {topic}")

    Args:
        ctx: The handler's context.
        text: The prompt.
        model: ``"anthropic/<model-id>"`` (or the model id alone). Defaults to
            ``$RESTATE_AGENT_MODEL``, else ``"anthropic/claude-opus-5-5"``.
        system: The system prompt.
        tools: ``restate.agents`` tools, or tool definitions of the Anthropic
            API (server tools, say).
        client: The Anthropic client. Defaults to one with credentials from the
            environment (``ANTHROPIC_API_KEY``, or an ``ant auth login`` profile).
        max_tokens: The most tokens of each reply. A reply cut off there fails
            the prompt with a ``TerminalError``.
        effort: How much effort the model puts into each reply (``"low"`` to
            ``"max"``). Defaults to the model's own default.
        fallbacks: Whether a request the model declines is answered by a
            fallback model the API picks. The Claude API supports it; turn it
            off for a client of Amazon Bedrock, Vertex AI or Microsoft Foundry.

    Returns:
        The model's answer.
    """
    options = _Options(model, system, tools, client, max_tokens, effort, fallbacks)

    async def read() -> Transcript:
        return await _transcript(ctx)

    async def work() -> str:
        return await _answer_once(ctx, text, options)

    return await transcribing(ctx, read, work)


def agent(
    name: str,
    *,
    model: PerSession[str | None] = None,
    system: PerSession[str | None] = None,
    tools: PerSession[Sequence[AnyTool]] = (),
    client: anthropic.AsyncAnthropic | None = None,
    max_tokens: int = 64_000,
    effort: Effort | None = None,
    fallbacks: bool = True,
    description: str | None = None,
    migrations: Sequence[Migration] = (),
    background: bool = False,
    metadata: dict[str, str] | None = None,
    inactivity_timeout: timedelta | None = None,
    abort_timeout: timedelta | None = None,
    ingress_private: bool | None = None,
    invocation_retry_policy: InvocationRetryPolicy | None = RETRIES,
) -> AgentObject:
    """Create a Claude agent: a virtual object with one conversation per key.

    Its ``chat`` handler answers each message with Claude on the Anthropic
    API; the conversation is kept in the key's database, a step at a time.
    Add handlers of your own with ``@assistant.handler``::

        assistant = anthropic.agent("assistant", system="Answer briefly.", tools=[get_weather])

    ``model``, ``system`` and ``tools`` may each be a function of the
    invocation's context, sync or async, for values that differ per user.
    The other options are as for ``prompt``.

    Args:
        name: The object's name, as Restate serves it.
        model: ``"anthropic/<model-id>"`` (or the model id alone). Defaults to
            ``$RESTATE_AGENT_MODEL``, else ``"anthropic/claude-opus-5-5"``.
        system: The system prompt.
        tools: ``restate.agents`` tools, or tool definitions of the Anthropic API.
        client: The Anthropic client; see ``prompt``.
        max_tokens: The most tokens of each reply; see ``prompt``.
        effort: How much effort the model puts into each reply; see ``prompt``.
        fallbacks: Whether declined requests go to a fallback model; see ``prompt``.
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
        TerminalError: If ``model`` names a provider other than Anthropic.
    """
    if model is None or isinstance(model, str):
        model_id_of(model_of(model, CLAUDE_MODEL), "anthropic")
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
            model=await configured(model, ctx),
            system=await configured(system, ctx),
            tools=await configured(tools, ctx),
            client=client,
            max_tokens=max_tokens,
            effort=effort,
            fallbacks=fallbacks,
        )

    obj.chat(answer)
    return obj
