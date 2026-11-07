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
  cut off.
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
  holding a process; a retried attempt joins the same waits. Static
  breakpoints (``interrupt_before``, ``interrupt_after``) have nothing to
  wait for: a graph that has them, or a subgraph LangGraph finds in it, is
  refused, and a run that stops at another subgraph's fails.
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
import weakref
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph
from langgraph.pregel import Pregel
from langgraph.types import Command, StateSnapshot
from pydantic_core import to_jsonable_python

from restate.agents import _signals
from restate.agents._common import from_tool
from restate.agents._conversation import Conversation, Entry, Run
from restate.agents._turn import stepped
from restate.agents.langgraph._checkpointer import SqlCheckpointSaver
from restate.agents.langgraph._context import running as running_graph
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
    """Raise ``TypeError`` if ``graph`` has no ``messages`` in its state, or it or a subgraph has static breakpoints.

    The subgraphs are those LangGraph finds: its nodes that are graphs, and
    a graph that a node calls, if the node names it. One that a node makes
    or calls otherwise is found only when its breakpoint stops a run, which
    then fails (see ``at_breakpoint``).
    """
    if "messages" not in graph.channels:
        raise TypeError("the graph's state has no messages: build it on LangGraph's MessagesState")
    compiled: Pregel[Any, Any, Any, Any] = graph.compile() if isinstance(graph, StateGraph) else graph
    # Untyped: LangGraph types a subgraph as a PregelProtocol of unknown parameters.
    subgraphs: list[tuple[str, object]] = list(typing.cast(Any, compiled).get_subgraphs(recurse=True))
    for each in [compiled, *(found for _, found in subgraphs)]:
        if isinstance(each, Pregel) and (each.interrupt_before_nodes or each.interrupt_after_nodes):
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


def _interrupts(snapshot: StateSnapshot, resumes: dict[str, int]) -> list[tuple[Any, int]]:
    """Return the pending interrupts of the run, with how often each was resumed (``resumes``, by interrupt id).

    LangGraph keeps each pending interrupt on the graph's task whose step
    raised it, a subgraph's interrupt included, even one of a node's second
    subgraph call, whose state LangGraph's subgraph snapshots do not show.
    How often it was resumed is counted at the task that raised it, in
    whichever namespace it ran (``SqlCheckpointSaver._resumes``).
    """
    return [(pending, resumes.get(pending.id, 0)) for task in snapshot.tasks for pending in task.interrupts]


async def _resumed(ctx: SessionContext, saver: SqlCheckpointSaver, snapshot: StateSnapshot) -> dict[str, Any] | None:
    """Wait for a signal for each pending interrupt of ``snapshot``, the state of the run's graph.

    Return their values by interrupt id, or ``None`` if no interrupt is
    pending. Each wait's id is the turn's
    invocation id (the same in each attempt), the interrupt's id, and the
    number of resume values its task already has: a node that interrupts
    twice has the same interrupt id both times, and each time is a wait of
    its own. A retried attempt joins the same waits; a turn that ends without
    them (killed, say) has its waits closed with it, as the companion closes
    the waits whose ids start with the turn's.
    """
    if not any(task.interrupts for task in snapshot.tasks):
        return None
    pending = _interrupts(snapshot, await saver._resumes(THREAD_ID))  # pyright: ignore[reportPrivateUsage]
    waits: dict[str, Any] = {}
    for interrupted, times in pending:
        if interrupted.id in waits:
            continue
        wait_id = f"{ctx.request.id}:interrupt:{interrupted.id}:{times}"
        request: _signals.WaitRequest = {
            "id": wait_id,
            "name": INTERRUPT,
            "input": to_jsonable_python(interrupted.value),
        }
        waits[interrupted.id] = ctx.call(_signals.wait, request, key=_signals.signals_of(ctx), idempotency_key=wait_id)
    values = await asyncio.gather(*waits.values())
    return dict(zip(waits, values, strict=True))


class _Errors(AsyncCallbackHandler):
    """Tell, in a graph's run, the errors that its chat models raised from those that its tools raised.

    Only a model's error can be its provider refusing the request (a wrong
    API key, say): any other is a tool's or a node's own, and a retry may
    get past it, whatever its name (a ``NotFoundError`` of another API,
    say). A model that a tool calls is the tool's.
    """

    def __init__(self, ctx: SessionContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.models: weakref.WeakSet[BaseException] = weakref.WeakSet()
        """The errors that the run's chat models raised."""

    async def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.models.add(error)

    async def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        from_tool(self.ctx, error)

    def mark(self, error: BaseException) -> None:
        """Mark ``error``, which the run failed with, as no refusal unless a chat model raised it."""
        if error not in self.models:
            from_tool(self.ctx, error)


def at_breakpoint(paused: StateSnapshot) -> bool:
    """Return whether a run stopped with steps to go and no interrupt: at a static breakpoint.

    An ``interrupt()`` in a subgraph is on the task of the graph that ran
    it (see ``_interrupts``): that run stopped at an interrupt.
    """
    return bool(paused.next) and not any(task.interrupts for task in paused.tasks)


async def _paused(app: Any) -> StateSnapshot:
    """Return the state of the graph's thread: where it stopped, and why (its tasks' interrupts)."""
    return typing.cast(StateSnapshot, await app.aget_state(THREAD))


def run_of(ctx: SessionContext, graph: AnyGraph, context: Any = None) -> Run:
    """Return the run of a prompt: the graph's, on the key's thread, from the prompt to its answer.

    ``context`` is the graph's run context, if not the handler's.
    """
    run_context = ctx if context is None else context

    async def run(conversation: Conversation) -> str:
        with running_graph(ctx):
            return await _run(conversation)

    async def _run(conversation: Conversation) -> str:
        running = conversation.running()
        assert running is not None and running.prompt is not None
        prompt_id = _prompt_id(running)
        saver = SqlCheckpointSaver(ctx.db, history="latest")
        app = _compiled(graph, saver)
        snapshot = await app.aget_state(THREAD)
        values = typing.cast(dict[str, Any], snapshot.values or {})
        messages = typing.cast(list[BaseMessage], values.get("messages", []))
        known = _known(conversation)
        metadata = typing.cast(dict[str, Any], snapshot.metadata or {})
        asked = metadata.get(_PROMPT) == prompt_id
        before = str(metadata.get(_STRUCTURED_BEFORE, "")) if asked else await _structured_version(app)
        # Each checkpoint of the run names its prompt, so that a retried attempt finds the run it cut off, and the
        # version its structured response had before it.
        errors = _Errors(ctx)
        config: RunnableConfig = {
            **THREAD,
            "metadata": {_PROMPT: prompt_id, _STRUCTURED_BEFORE: before},
            "callbacks": [errors],
        }
        if asked and not snapshot.next:
            # The run had finished, but its answer was not stored: that is the answer, rather than run again.
            await _copied(ctx, conversation, known, messages)
            return await _answer(app, conversation, running, values, before)
        # A run cut off by a crash is resumed: first by the signals its pending interrupts wait for, if any. A new
        # message (or one after a stopped run) starts a run, after what closed the previous run.
        start: Any
        if asked:
            resumes = await _resumed(ctx, saver, await _paused(app))
            start = Command(resume=resumes) if resumes is not None else None
        else:
            prompt = HumanMessage(str(typing.cast(BaseMessage, running.item).text), id=prompt_id)
            start = {"messages": [*_closing(conversation, running, messages), prompt]}
        while True:
            states = app.astream(start, config, context=run_context, durability="sync", stream_mode="values")
            try:
                async for state in states:
                    values = typing.cast(dict[str, Any], state)
                    await _copied(ctx, conversation, known, typing.cast(list[BaseMessage], values.get("messages", [])))
            except Exception as error:
                errors.mark(error)
                raise
            # The run stops at its interrupts: it goes on with the signals that answer them.
            paused = await _paused(app)
            resumes = await _resumed(ctx, saver, paused)
            if resumes is None:
                if at_breakpoint(paused):
                    # At the static breakpoint of a subgraph that check() could not find. Fail, rather than
                    # answer with a run half done.
                    raise TerminalError(BREAKPOINTS)
                return await _answer(app, conversation, running, values, before)
            start = Command(resume=resumes)

    return run
