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
  from the state the graph had stored.
"""

from __future__ import annotations

import typing
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph
from langgraph.pregel import Pregel

from restate.agents._conversation import Conversation, Run
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


def run_of(ctx: SessionContext, graph: AnyGraph) -> Run:
    """Return the run of a prompt: the graph's, on the key's thread, from the prompt to its answer."""

    async def run(conversation: Conversation) -> str:
        running = conversation.running()
        assert running is not None and running.prompt is not None
        # The prompt's id is the conversation's, so that a retried attempt finds it in the graph's state.
        prompt_id = f"prompt:{running.prompt}"
        app = _compiled(graph, ctx)
        snapshot = await app.aget_state(THREAD)
        messages = typing.cast(list[BaseMessage], (snapshot.values or {}).get("messages", []))
        asked = any(m.id == prompt_id for m in messages)
        if asked and not snapshot.next:
            # The run had finished, but its answer was not stored: that is the answer, rather than run again.
            await _copied(ctx, conversation, _after(messages, prompt_id))
            return _answer(_after(messages, prompt_id))
        # A run cut off by a crash is resumed. A new message (or one after a stopped run) starts a run.
        prompt = HumanMessage(str(typing.cast(BaseMessage, running.item).text), id=prompt_id)
        start = None if asked else {"messages": [prompt]}
        async for state in app.astream(start, THREAD, context=ctx, durability="sync", stream_mode="values"):
            messages = typing.cast(list[BaseMessage], typing.cast(dict[str, Any], state).get("messages", []))
            await _copied(ctx, conversation, _after(messages, prompt_id))
        return _answer(_after(messages, prompt_id))

    return run
