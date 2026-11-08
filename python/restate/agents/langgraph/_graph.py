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
  stored before each step runs (``durability="sync"``), and the prebuilt
  ``nodes`` and the tools of ``tools()`` get the handler's context in it
  (``_context.py``). LangGraph's run context is the graph's own
  (``context=``), else the handler's. A retried attempt resumes the run it
  cut off. A run that finishes prunes the thread to the latest checkpoint
  of each namespace (LangGraph's ``aprune``).
- Any error of the run fails the attempt, and Restate retries it: none is
  taken for a model provider's refusal, as the built-in agent's model's can
  be, since a graph's models, tools and nodes are all its own (a tool or a
  node may call a model too). A ``TerminalError`` ends the turn, and so do
  LangGraph's own errors that a retry would meet again, such as a run past
  its recursion limit (``_errors.py``). A run that an invalid update ended
  leaves writes that LangGraph cannot apply: the next message starts its run
  from the checkpoint before them, as a new run discards them.
- After each step, the AI and tool messages the step added are copied into
  the agent's conversation, by their ids, so that the ``messages``, ``sync``
  and ``watch`` views show the run as it goes, as they do for the built-in
  agent. They are the messages after the run's prompt: the graph's state
  holds the earlier turns too, whose messages the conversation has, but
  does not load (it loads the previous turn whole, at least). A graph that
  trims the prompt away has the messages it keeps copied if their ids are
  not the loaded turns', as a graph that trims keeps the latest messages.

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
  holding a process; a retried attempt joins the same waits. Static
  breakpoints (``interrupt_before``, ``interrupt_after``) have nothing to
  wait for: a compiled graph that has them is refused, and a run that stops
  at a subgraph's fails.
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
from langgraph.errors import InvalidUpdateError
from langgraph.graph import StateGraph
from langgraph.pregel import Pregel
from langgraph.types import Command, StateSnapshot
from pydantic_core import to_jsonable_python

from restate.agents import _signals
from restate.agents._common import from_tool
from restate.agents._conversation import Conversation, Run, Turn
from restate.agents._turn import stepped
from restate.agents.langgraph._checkpointer import SqlCheckpointSaver
from restate.agents.langgraph._context import running as running_graph
from restate.agents.langgraph._errors import terminal_of
from restate.exceptions import TerminalError
from restate.sessions import SessionContext

AnyGraph: typing.TypeAlias = StateGraph[Any, Any, Any, Any] | Pregel[Any, Any, Any, Any]
"""A graph of your own: a ``StateGraph`` (uncompiled), or a compiled one such as LangChain's ``create_agent`` makes."""

THREAD_ID = "chat"
"""The graph's thread: there is one per key, as the key's database is its own."""

THREAD: RunnableConfig = {"configurable": {"thread_id": THREAD_ID}}


BREAKPOINTS = (
    "static breakpoints (interrupt_before, interrupt_after) stop a run with nothing to wait for, and leave its tool "
    "calls without results: pause the graph with interrupt() instead, which waits for a signal"
)
"""Why a graph with static breakpoints is refused, and a run stopped at a subgraph's fails."""


def check(graph: AnyGraph) -> None:
    """Raise ``TypeError`` if ``graph`` has no ``messages`` in its state, or is compiled with static breakpoints.

    A subgraph's static breakpoints are found when one stops a run, which
    then fails (see ``at_breakpoint``): LangGraph has no public way to find
    every subgraph (a node may call one it makes).
    """
    if "messages" not in graph.channels:
        raise TypeError("the graph's state has no messages: build it on LangGraph's MessagesState")
    if isinstance(graph, Pregel) and (graph.interrupt_before_nodes or graph.interrupt_after_nodes):
        raise TypeError(BREAKPOINTS)


def _compiled(graph: AnyGraph, saver: SqlCheckpointSaver) -> Any:
    """Return ``graph`` compiled with its checkpoints in ``saver`` (untyped: LangGraph's run context is ours)."""
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


async def _answer(app: Any, turn: Turn, state: dict[str, Any], before: str) -> str:
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
        for e in turn.items
        if isinstance(item := typing.cast(BaseMessage, e.item), AIMessage) and item.id is not None
    ]
    return str(copied[-1].text) if copied else ""


async def _copied(
    ctx: SessionContext, turn: Turn, known: set[str], messages: Sequence[BaseMessage], prompt: str
) -> None:
    """Copy the AI and tool messages of ``messages`` (the graph's state) that the conversation does not have yet.

    Those are the messages after the run's prompt (whose id is ``prompt``),
    as the graph's state holds the earlier turns too, which the conversation
    may not have loaded. If the graph trimmed the prompt away, they are the
    messages whose ids are not ``known``. ``known`` is the ids the
    conversation has, which this adds to; the messages the conversation gave
    the graph are not copied back. Then publish.
    """
    at = next((i for i, m in enumerate(messages) if m.id == prompt), None)
    new = [
        m
        for m in (messages if at is None else messages[at + 1 :])
        if isinstance(m, AIMessage | ToolMessage)
        and m.id is not None
        and m.id not in known
        and not m.id.startswith(_CARRIED)
    ]
    for message in new:
        if isinstance(message, AIMessage):
            await turn.append_reply(message)
        else:
            await turn.append(message)
        known.add(typing.cast(str, message.id))
    if new:
        await stepped(ctx)


def _prompt_id(turn: Turn) -> str:
    """Return the id of a turn's prompt, as a ``HumanMessage`` in the graph: the conversation's id of the turn."""
    return f"prompt:{turn.id}"


def _closing(conversation: Conversation, turn: Turn, messages: Sequence[BaseMessage]) -> list[BaseMessage]:
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
    turns = list(conversation.turns)
    at = turns.index(turn)
    if at == 0:
        return []
    previous = turns[at - 1]
    known = {m.id for m in messages}
    copied = any(typing.cast(BaseMessage, e.item).id is not None for e in previous.items)
    # Only a turn closed by the conversation (stopped, refused, failed, ended) has messages the graph lacks: its
    # closing record. An answered turn's own are the graph's; the answer it ends with, if the conversation added it
    # (the graph ended with a tool's message, say), is the conversation's.
    closed = previous.status not in (None, "answered")
    carried: list[BaseMessage] = []
    for entry in previous.entries:
        item = typing.cast(BaseMessage, entry.item)
        if entry.prompt is not None:
            if not copied and _prompt_id(previous) not in known:
                carried.append(HumanMessage(str(item.text), id=_prompt_id(previous)))
        elif closed and not isinstance(item, HumanMessage) and item.id is None and f"closed:{entry.n}" not in known:
            carried.append(item.model_copy(update={"id": f"closed:{entry.n}"}))
    return carried


INTERRUPT = "interrupt"
"""The name of the signal that answers LangGraph's ``interrupt()``."""


def _interrupts(snapshot: StateSnapshot) -> list[Any]:
    """Return the pending interrupts of the run, each once.

    LangGraph keeps each pending interrupt on the graph's task whose step
    raised it, a subgraph's interrupt included, even one of a node's second
    subgraph call, whose state LangGraph's subgraph snapshots do not show.
    Each graph above the task that raised it lists it again, under the same
    id. That id is the raiser's namespace and the number of resume values
    its task already has: each ``interrupt()`` of a node that interrupts
    twice has an id of its own.
    """
    pending: dict[str, Any] = {}
    for task in snapshot.tasks:
        for interrupted in task.interrupts:
            pending.setdefault(interrupted.id, interrupted)
    return list(pending.values())


async def _resumed(ctx: SessionContext, snapshot: StateSnapshot) -> dict[str, Any] | None:
    """Wait for a signal for each pending interrupt of ``snapshot``, the state of the run's graph.

    Return their values by interrupt id, or ``None`` if no interrupt is
    pending. Each wait's id is the turn's invocation id (the same in each
    attempt) and the interrupt's id, which LangGraph gives each
    ``interrupt()`` of a run (see ``_interrupts``). A retried attempt joins
    the same waits; a turn that ends without them (killed, say) has its
    waits closed with it, as the companion closes the waits whose ids start
    with the turn's.
    """
    pending = _interrupts(snapshot)
    if not pending:
        return None
    waits: dict[str, Any] = {}
    for interrupted in pending:
        wait_id = f"{ctx.request.id}:interrupt:{interrupted.id}"
        request: _signals.WaitRequest = {
            "id": wait_id,
            "name": INTERRUPT,
            "input": to_jsonable_python(interrupted.value),
        }
        waits[interrupted.id] = ctx.call(_signals.wait, request, key=_signals.signals_of(ctx), idempotency_key=wait_id)
    values = await asyncio.gather(*waits.values())
    return dict(zip(waits, values, strict=True))


def at_breakpoint(paused: StateSnapshot) -> bool:
    """Return whether a run stopped with steps to go and no interrupt: at a static breakpoint.

    An ``interrupt()`` in a subgraph is on the task of the graph that ran
    it (see ``_interrupts``): that run stopped at an interrupt.
    """
    return bool(paused.next) and not any(task.interrupts for task in paused.tasks)


async def _stored(app: Any) -> StateSnapshot:
    """Return the state of the graph's thread: where it stopped, and why (its tasks' interrupts).

    LangGraph shows the state with the writes of the last step's tasks
    applied. If they do not fit the state (an invalid update), it is the
    state of the checkpoint itself, which LangGraph shows without them. A run
    with new input discards them, and resuming the run that made them (a
    retried attempt, after a crash before its turn ended) fails at them, with
    the terminal error.
    """
    try:
        return typing.cast(StateSnapshot, await app.aget_state(THREAD))
    except InvalidUpdateError:
        latest = await app.checkpointer.aget_tuple(THREAD)
        if latest is None:
            raise
        return typing.cast(StateSnapshot, await app.aget_state(latest.config))


def run_of(ctx: SessionContext, graph: AnyGraph, context: Any = None) -> Run:
    """Return the run of a prompt: the graph's, on the key's thread, from the prompt to its answer.

    ``context`` is the graph's run context, if not the handler's.
    """
    run_context = ctx if context is None else context

    async def run(conversation: Conversation) -> str:
        with running_graph(ctx):
            return await _run(conversation)

    async def _run(conversation: Conversation) -> str:
        turn = conversation.turn
        # The run's input: the turn's prompt, or the message that asked again within the turn, under an id of its
        # own that no prompt has (``next_prompt_id`` makes ``<id>:<k>`` only).
        step = turn.latest_ask()
        prompt_id = _prompt_id(turn) if step is turn.prompt else f"{_prompt_id(turn)}:again:{step.n}"
        saver = SqlCheckpointSaver(ctx.db)
        app = _compiled(graph, saver)
        snapshot = await _stored(app)
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
            await _copied(ctx, turn, known, messages, prompt_id)
            await saver.aprune([THREAD_ID])
            return await _answer(app, turn, values, before)
        # A run cut off by a crash is resumed: first by the signals its pending interrupts wait for, if any. A new
        # message (or one after a stopped run) starts a run, after what closed the previous run.
        start: Any
        if asked:
            resumes = await _resumed(ctx, await _stored(app))
            start = Command(resume=resumes) if resumes is not None else None
        else:
            prompt = HumanMessage(str(typing.cast(BaseMessage, step.item).text), id=prompt_id)
            closing = _closing(conversation, turn, messages) if step is turn.prompt else []
            start = {"messages": [*closing, prompt]}
        while True:
            states = app.astream(start, config, context=run_context, durability="sync", stream_mode="values")
            try:
                async for state in states:
                    values = typing.cast(dict[str, Any], state)
                    await _copied(
                        ctx, turn, known, typing.cast(list[BaseMessage], values.get("messages", [])), prompt_id
                    )
            except Exception as error:
                if (terminal := terminal_of(error)) is not None:
                    # LangGraph's own error, which a retry would meet again: a run past its recursion limit, say.
                    raise terminal from error
                # No refusal, whatever its name: a graph's models, tools and nodes are all its own.
                from_tool(ctx, error)
                raise
            # The run stops at its interrupts: it goes on with the signals that answer them.
            paused = await _stored(app)
            resumes = await _resumed(ctx, paused)
            if resumes is None:
                if at_breakpoint(paused):
                    # At a subgraph's static breakpoint, which check() does not look for. Fail, rather than
                    # answer with a run half done.
                    raise TerminalError(BREAKPOINTS)
                # Only the latest checkpoint of each namespace is read again; a paused or cut-off run keeps it too.
                await saver.aprune([THREAD_ID])
                return await _answer(app, turn, values, before)
            start = Command(resume=resumes)

    return run
