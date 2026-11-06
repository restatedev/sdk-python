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
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
"""An agent whose ``chat`` runs a LangGraph graph of your own: ``langgraph.agent(name, graph=...)``.

The graph's state has LangGraph's ``messages`` (``MessagesState``), and each
message to the agent is a run of the graph on the key's thread, with the
message as a new ``HumanMessage``:

- The graph's checkpoints are in the key's database (``SqlCheckpointSaver``),
  stored before each step runs (``durability="sync"``), and the run's
  context is the handler's: the prebuilt ``nodes`` work in it. A retried
  attempt resumes the run it cut off.
- After each step, the AI and tool messages the step added are copied into
  the agent's conversation, by their ids, so that the ``messages``, ``sync``
  and ``watch`` views show the run as it goes, as they do for the built-in
  agent.

  The conversation never gets ahead of the checkpoints: with
  ``durability="sync"``, LangGraph streams a step's state only once its
  checkpoint is stored, so a message is copied only once the graph has it for
  good (no crash can make the step run again and say something else). The
  checkpoints can get ahead of the conversation, if a crash comes between a
  step's checkpoint and its copy: the next step's state, or the end of the
  run, copies what was missed, as messages are copied by id. The
  conversation is a log: a message the graph removes or edits later stays as
  it was copied.
- The answer is the text of the run's last AI message. A graph that writes a
  ``structured_response`` in the run (LangChain's ``create_agent`` with a
  ``response_format``) answers with it instead, as JSON, which ``output=``
  validates; one left from an earlier run is not the answer.
- LangGraph's ``interrupt()`` waits for a signal, as ``wait_for_signal``
  does: each pending interrupt is a wait named ``"interrupt"``, which the
  agent's ``pending`` view lists with the interrupt's value, and which its
  ``signal`` view resolves, by the wait's id or as the oldest
  ``"interrupt"``. The run then resumes with ``Command(resume=...)`` and the
  signal's value, as ``interrupt()``'s result. The turn waits without
  holding a process; a retried attempt joins the same waits.
- The graph may trim its history, its input and answer included (as a
  summarizing graph does): a run is found by its checkpoints' metadata,
  which name its prompt, and its answer is read from the conversation.
- A stopped run is not resumed: the next message starts a run of its own,
  from the state the graph had stored. What closed the stopped (or refused,
  or failed) run in the conversation comes first in the new run's input: the
  results of its cut-off tool calls, so that no tool call is left without one
  (a model provider refuses such a history), and the note that ends it (such
  as "Cancelled: ..."), so that the graph's model knows, as the built-in
  agent's does. So does that run's message, if the graph never stored it.
"""

from __future__ import annotations

import asyncio
import json
import typing
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph
from langgraph.pregel import Pregel
from langgraph.types import Command, StateSnapshot
from pydantic_core import to_jsonable_python

from restate.agents import _signals
from restate.agents._conversation import Conversation, Entry, Run
from restate.agents._turn import stepped
from restate.agents.langgraph._checkpointer import SqlCheckpointSaver
from restate.sessions import SessionContext

AnyGraph: typing.TypeAlias = StateGraph[Any, Any, Any, Any] | Pregel[Any, Any, Any, Any]
"""A graph of your own: a ``StateGraph`` (uncompiled), or a compiled one such as LangChain's ``create_agent`` makes."""

THREAD: RunnableConfig = {"configurable": {"thread_id": "chat"}}
"""The graph's thread: there is one per key, as the key's database is its own."""


def check(graph: AnyGraph) -> None:
    """Raise ``TypeError`` if ``graph`` has no ``messages`` in its state."""
    if "messages" not in graph.channels:
        raise TypeError("the graph's state has no messages: build it on LangGraph's MessagesState")


def _compiled(graph: AnyGraph, ctx: SessionContext) -> Any:
    """Return ``graph`` compiled with its checkpoints in the key's database (untyped: its context is ``ctx``)."""
    saver = SqlCheckpointSaver(ctx.db, history="latest")
    if isinstance(graph, StateGraph):
        return graph.compile(checkpointer=saver)
    return graph.copy(update={"checkpointer": saver})


_PROMPT = "restate_prompt"
"""The key, in each checkpoint's metadata, of the prompt whose run made it: the run's input may be trimmed away."""

_STRUCTURED = "structured_response"
"""The state field of a structured answer, as LangChain's ``create_agent`` writes it."""

_STRUCTURED_BEFORE = "restate_structured_version"
"""The key, in each checkpoint's metadata, of the version the ``structured_response`` channel had as its run began."""

_CARRIED = ("prompt:", "closed:")
"""The prefixes of the ids of the messages that the conversation gave the graph, rather than the graph made."""


def _known(conversation: Conversation) -> set[str]:
    """Return the ids of the graph's messages that the conversation has: those copied from it."""
    return {i for e in conversation.entries if (i := typing.cast(BaseMessage, e.item).id) is not None}


async def _structured_version(app: Any) -> str:
    """Return the version of the graph's ``structured_response`` channel, which changes each time it is written."""
    stored = await app.checkpointer.aget_tuple(THREAD)
    versions = typing.cast(dict[str, Any], stored.checkpoint["channel_versions"]) if stored is not None else {}
    return str(versions.get(_STRUCTURED, ""))


async def _answer(app: Any, conversation: Conversation, running: Entry, state: dict[str, Any], before: str) -> str:
    """Return the run's answer: the JSON of the ``structured_response`` it wrote, else its last AI message's text.

    A ``structured_response`` counts only if the run wrote it (its channel's
    version is not ``before``): the state keeps an earlier run's. The text is
    read from the conversation, which is a log, as the graph may have trimmed
    its own history (the run's input and answer included).
    """
    structured = state.get(_STRUCTURED)
    if structured is not None and await _structured_version(app) != before:
        return json.dumps(to_jsonable_python(structured))
    copied = [
        item
        for e in conversation.entries
        if e.n > running.n and isinstance(item := typing.cast(BaseMessage, e.item), AIMessage) and item.id is not None
    ]
    return str(copied[-1].text) if copied else ""


async def _copied(
    ctx: SessionContext, conversation: Conversation, known: set[str], messages: Sequence[BaseMessage]
) -> None:
    """Copy the AI and tool messages of ``messages`` (the graph's state) that the conversation does not have yet.

    ``known`` is the ids the conversation has, which this adds to; the
    messages the conversation gave the graph are not copied back. Then
    publish.
    """
    new = [
        m
        for m in messages
        if isinstance(m, AIMessage | ToolMessage)
        and m.id is not None
        and m.id not in known
        and not m.id.startswith(_CARRIED)
    ]
    for message in new:
        if isinstance(message, AIMessage):
            await conversation.append_reply(message)
        else:
            await conversation.append(message)
        known.add(typing.cast(str, message.id))
    if new:
        await stepped(ctx)


def _prompt_id(entry: Entry) -> str:
    """Return the id of a prompt's ``HumanMessage`` in the graph: the conversation's id of the prompt."""
    return f"prompt:{entry.prompt}"


def _closing(conversation: Conversation, running: Entry, messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """Return what the conversation has of the previous prompt's run that the graph does not.

    That is what closed the run, if it was stopped, refused or failed: the
    results of its cut-off tool calls and the note that ends it, which only
    the conversation has (copied messages have the graph's ids; these have
    none). And the previous prompt itself, if the graph never stored it: if
    its run copied nothing (a run that copied a message had stored its
    prompt). A prompt the graph stored and then removed (trimming its
    history, say) is not brought back. Each gets an id of its own, so that
    it is carried once.
    """
    earlier = [e for e in conversation.entries if e.prompt is not None and e.n < running.n]
    if not earlier:
        return []
    previous = earlier[-1]
    known = {m.id for m in messages}
    between = [e for e in conversation.entries if previous.n <= e.n < running.n]
    copied = any(e.prompt is None and typing.cast(BaseMessage, e.item).id is not None for e in between)
    carried: list[BaseMessage] = []
    for entry in between:
        item = typing.cast(BaseMessage, entry.item)
        if entry.prompt is not None:
            if not copied and _prompt_id(entry) not in known:
                carried.append(HumanMessage(str(item.text), id=_prompt_id(entry)))
        elif item.id is None and f"closed:{entry.n}" not in known:
            carried.append(item.model_copy(update={"id": f"closed:{entry.n}"}))
    return carried


INTERRUPT = "interrupt"
"""The name of the signal that answers LangGraph's ``interrupt()``."""

_RESUME = "__resume__"
"""LangGraph's channel of a task's resume values, among a checkpoint's pending writes."""


async def _interrupts(app: Any, snapshot: StateSnapshot) -> list[tuple[Any, int]]:
    """Return the pending interrupts of ``snapshot`` (a graph's or a subgraph's), with how often each was resumed.

    An interrupt is counted at the task that raised it, inside subgraphs,
    whose resume values are among the pending writes of its own graph's
    checkpoint.
    """
    stored = await app.checkpointer.aget_tuple(snapshot.config)
    writes = typing.cast(list[tuple[str, str, Any]], stored.pending_writes if stored is not None else [])
    resumed: dict[str, int] = {}
    for task_id, channel, values in writes:
        if channel == _RESUME:
            count = len(typing.cast(list[Any], values)) if isinstance(values, list) else 1
            resumed[task_id] = max(resumed.get(task_id, 0), count)
    found: list[tuple[Any, int]] = []
    for task in snapshot.tasks:
        if not task.interrupts:
            continue
        if isinstance(task.state, StateSnapshot):
            found.extend(await _interrupts(app, task.state))
        else:
            found.extend((pending, resumed.get(task.id, 0)) for pending in task.interrupts)
    return found


async def _resumed(ctx: SessionContext, app: Any) -> dict[str, Any] | None:
    """Wait for a signal for each of the graph's pending interrupts, and return their values by interrupt id.

    Return ``None`` if no interrupt is pending. Each wait's id is the
    interrupt's, and the number of resume values its task already has: a
    node that interrupts twice has the same interrupt id both times, and
    each time is a wait of its own. A retried attempt joins the same waits.
    """
    pending = await _interrupts(app, typing.cast(StateSnapshot, await app.aget_state(THREAD, subgraphs=True)))
    if not pending:
        return None
    waits: dict[str, Any] = {}
    for interrupted, times in pending:
        if interrupted.id in waits:
            continue
        wait_id = f"langgraph:{ctx.key}:{interrupted.id}:{times}"
        request: _signals.WaitRequest = {
            "id": wait_id,
            "name": INTERRUPT,
            "input": to_jsonable_python(interrupted.value),
        }
        waits[interrupted.id] = ctx.call(_signals.wait, request, key=_signals.signals_of(ctx), idempotency_key=wait_id)
    values = await asyncio.gather(*waits.values())
    return dict(zip(waits, values, strict=True))


def run_of(ctx: SessionContext, graph: AnyGraph) -> Run:
    """Return the run of a prompt: the graph's, on the key's thread, from the prompt to its answer."""

    async def run(conversation: Conversation) -> str:
        running = conversation.running()
        assert running is not None and running.prompt is not None
        prompt_id = _prompt_id(running)
        app = _compiled(graph, ctx)
        snapshot = await app.aget_state(THREAD)
        values = typing.cast(dict[str, Any], snapshot.values or {})
        messages = typing.cast(list[BaseMessage], values.get("messages", []))
        known = _known(conversation)
        metadata = typing.cast(dict[str, Any], snapshot.metadata or {})
        asked = metadata.get(_PROMPT) == prompt_id
        before = str(metadata.get(_STRUCTURED_BEFORE, "")) if asked else await _structured_version(app)
        # Each checkpoint of the run names its prompt, so that a retried attempt finds the run it cut off, and the
        # version its structured response had before it.
        config: RunnableConfig = {**THREAD, "metadata": {_PROMPT: prompt_id, _STRUCTURED_BEFORE: before}}
        if asked and not snapshot.next:
            # The run had finished, but its answer was not stored: that is the answer, rather than run again.
            await _copied(ctx, conversation, known, messages)
            return await _answer(app, conversation, running, values, before)
        # A run cut off by a crash is resumed: first by the signals its pending interrupts wait for, if any. A new
        # message (or one after a stopped run) starts a run, after what closed the previous run.
        start: Any
        if asked:
            resumes = await _resumed(ctx, app)
            start = Command(resume=resumes) if resumes is not None else None
        else:
            prompt = HumanMessage(str(typing.cast(BaseMessage, running.item).text), id=prompt_id)
            start = {"messages": [*_closing(conversation, running, messages), prompt]}
        while True:
            async for state in app.astream(start, config, context=ctx, durability="sync", stream_mode="values"):
                values = typing.cast(dict[str, Any], state)
                await _copied(ctx, conversation, known, typing.cast(list[BaseMessage], values.get("messages", [])))
            # The run stops at its interrupts: it goes on with the signals that answer them.
            resumes = await _resumed(ctx, app)
            if resumes is None:
                return await _answer(app, conversation, running, values, before)
            start = Command(resume=resumes)

    return run
