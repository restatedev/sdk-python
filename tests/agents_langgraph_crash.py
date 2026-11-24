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
"""A graph of your own through crashes (``langgraph.prompt(ctx, text, graph=...)``): a transfer moves money once, and
the prompt is answered once, wherever the attempt crashes. In process: a crash is a sync of the object's state that
fails, with the writes it had not yet stored lost."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import collections
from typing import Annotated, Any

import pytest

pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.tools import InjectedToolCallId, tool  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.prebuilt import ToolNode, tools_condition  # noqa: E402

from restate import sessions  # noqa: E402
from restate.agents import langgraph  # noqa: E402
from restate.agents.langgraph import SqlCheckpointSaver  # noqa: E402
from sessions_database import Backend  # noqa: E402  # pylint: disable=import-error
from sessions_object import Host, request  # noqa: E402  # pylint: disable=import-error
from restate.sessions.internal import invoke  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

moved: collections.Counter[str] = collections.Counter()
"""The transfers made, by the call that asked for each: a transfer is made once per call, as a journaled effect is."""


@tool
def transfer(to: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> str:
    """Move 10 to an account."""
    moved[tool_call_id] += 1
    return f"moved 10 to {to}"


def teller(state: MessagesState) -> dict[str, Any]:
    """The scripted model: it asks for a transfer, and once it has its result, answers."""
    messages = state["messages"]
    asked = next(m for m in reversed(messages) if isinstance(m, HumanMessage))
    after = messages[messages.index(asked) + 1 :]
    if any(isinstance(m, ToolMessage) for m in after):
        return {"messages": [AIMessage("all done")]}
    call = {"id": f"call-{asked.id}", "name": "transfer", "args": {"to": "bob"}}
    return {"messages": [AIMessage("", tool_calls=[call])]}


def banking() -> StateGraph:
    """A graph of a model and a node of tools, as LangChain's ``create_agent`` builds."""
    graph = StateGraph(MessagesState)
    graph.add_node("model", teller)
    graph.add_node("tools", ToolNode([transfer]))
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "model")
    return graph


def desk(graph: StateGraph, name: str = "asker") -> sessions.SessionObject:
    obj = sessions.SessionObject(name)

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await langgraph.prompt(ctx, text, graph=graph)

    return obj


async def test_a_graph_answers_a_prompt_once_after_a_crash_at_any_of_its_syncs():
    obj = desk(banking())
    crashed = 0
    for syncs in range(200):
        moved.clear()
        backend, invocation = Backend(), request()
        crashing = Host(backend, service="asker")
        crashing.crash_after(syncs)
        try:
            assert await invoke(obj, "ask", crashing, invocation, "pay bob") == "all done"
            break
        except RuntimeError as error:
            assert str(error) == "the sync failed"
        crashed += 1
        answer = await invoke(obj, "ask", Host(backend, service="asker"), invocation, "pay bob")
        # One call, one transfer: a tool that a crash cut off runs again with the same call id (Restate's journal
        # answers it), but no second call is made.
        assert (syncs, answer, len(moved)) == (syncs, "all done", 1)
    assert crashed > 0


async def test_a_run_whose_last_steps_writes_are_stored_but_not_its_checkpoint_goes_on_from_them():
    """The state of a crash between a step's writes and its checkpoint: the task's writes are stored (here in place of
    the error it stored), and no checkpoint follows them."""
    ran: list[str] = []
    crash = [True]

    def node(name: str) -> Any:
        def step(state: MessagesState) -> dict[str, Any]:
            ran.append(name)
            if name == "second" and crash[0]:
                crash[0] = False
                raise RuntimeError("the process died")
            return {"messages": [AIMessage(f"{name} done")]}

        return step

    graph = StateGraph(MessagesState)
    for name in ("first", "second", "third"):
        graph.add_node(name, node(name))
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", "third")
    graph.add_edge("third", END)
    obj = desk(graph)

    @obj.handler
    async def seed(ctx: sessions.SessionContext) -> list[str]:
        saver = SqlCheckpointSaver(ctx.db)
        snapshot = await graph.compile(checkpointer=saver).aget_state({"configurable": {"thread_id": "chat"}})
        assert [task.name for task in snapshot.tasks] == ["second"]
        await ctx.db.execute("DELETE FROM restate_langgraph_writes WHERE channel = '__error__'")
        # What a node's task writes: its update, and the trigger of the node that follows.
        writes: list[tuple[str, Any]] = [
            ("messages", [AIMessage("second done", id="second_done")]),
            ("branch:to:third", None),
        ]
        await saver.aput_writes(snapshot.config, writes, snapshot.tasks[0].id)
        return [task.name for task in snapshot.tasks]

    backend, invocation = Backend(), request()
    with pytest.raises(RuntimeError, match="the process died"):
        await invoke(obj, "ask", Host(backend, service="asker"), invocation, "go")
    await invoke(obj, "seed", Host(backend, service="asker"), request())
    assert await invoke(obj, "ask", Host(backend, service="asker"), invocation, "go") == "third done"
    assert ran == ["first", "second", "third"]
