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
- The answer is the text of the run's last AI message.
- A stopped run is not resumed: the next message starts a run of its own,
  from the state the graph had stored. What closed the stopped (or refused,
  or failed) run in the conversation comes first in the new run's input: the
  results of its cut-off tool calls, so that no tool call is left without one
  (a model provider refuses such a history), and the note that ends it (such
  as "Cancelled: ..."), so that the graph's model knows, as the built-in
  agent's does. So does that run's message, if the graph never stored it.
"""

from __future__ import annotations

import typing
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph
from langgraph.pregel import Pregel

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


def _after(messages: Sequence[BaseMessage], prompt_id: str) -> list[BaseMessage]:
    """Return the messages after the prompt ``prompt_id``: those of its run."""
    at = next((i for i, m in enumerate(messages) if m.id == prompt_id), None)
    return list(messages[at + 1 :]) if at is not None else []


def _answer(messages: Sequence[BaseMessage]) -> str:
    """Return the text of the last AI message of a run."""
    replies = [m for m in messages if isinstance(m, AIMessage)]
    return str(replies[-1].text) if replies else ""


async def _copied(ctx: SessionContext, conversation: Conversation, run: Sequence[BaseMessage]) -> None:
    """Copy the AI and tool messages of ``run`` that the conversation does not have yet, and publish them."""
    known = {typing.cast(BaseMessage, entry.item).id for entry in conversation.entries}
    new = [m for m in run if isinstance(m, AIMessage | ToolMessage) and (m.id is None or m.id not in known)]
    for message in new:
        if isinstance(message, AIMessage):
            await conversation.append_reply(message)
        else:
            await conversation.append(message)
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
    none). And the previous prompt itself, if the graph never stored it. Each
    gets an id of its own, so that it is carried once.
    """
    earlier = [e for e in conversation.entries if e.prompt is not None and e.n < running.n]
    if not earlier:
        return []
    previous = earlier[-1]
    known = {m.id for m in messages}
    carried: list[BaseMessage] = []
    for entry in conversation.entries:
        if not previous.n <= entry.n < running.n:
            continue
        item = typing.cast(BaseMessage, entry.item)
        if entry.prompt is not None:
            if _prompt_id(entry) not in known:
                carried.append(HumanMessage(str(item.text), id=_prompt_id(entry)))
        elif item.id is None and f"closed:{entry.n}" not in known:
            carried.append(item.model_copy(update={"id": f"closed:{entry.n}"}))
    return carried


def run_of(ctx: SessionContext, graph: AnyGraph) -> Run:
    """Return the run of a prompt: the graph's, on the key's thread, from the prompt to its answer."""

    async def run(conversation: Conversation) -> str:
        running = conversation.running()
        assert running is not None and running.prompt is not None
        # The prompt's id is the conversation's, so that a retried attempt finds it in the graph's state.
        prompt_id = _prompt_id(running)
        app = _compiled(graph, ctx)
        snapshot = await app.aget_state(THREAD)
        messages = typing.cast(list[BaseMessage], (snapshot.values or {}).get("messages", []))
        asked = any(m.id == prompt_id for m in messages)
        if asked and not snapshot.next:
            # The run had finished, but its answer was not stored: that is the answer, rather than run again.
            await _copied(ctx, conversation, _after(messages, prompt_id))
            return _answer(_after(messages, prompt_id))
        # A run cut off by a crash is resumed. A new message (or one after a stopped run) starts a run, after what
        # closed the previous run.
        prompt = HumanMessage(str(typing.cast(BaseMessage, running.item).text), id=prompt_id)
        start = None if asked else {"messages": [*_closing(conversation, running, messages), prompt]}
        async for state in app.astream(start, THREAD, context=ctx, durability="sync", stream_mode="values"):
            messages = typing.cast(list[BaseMessage], typing.cast(dict[str, Any], state).get("messages", []))
            await _copied(ctx, conversation, _after(messages, prompt_id))
        return _answer(_after(messages, prompt_id))

    return run
