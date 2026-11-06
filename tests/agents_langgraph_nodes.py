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
"""LangGraph's prebuilt Restate nodes (``restate.agents.langgraph.nodes``), against a real Restate server.

A graph of your own, run by a handler of an agent, waits for a signal, runs a
workflow and asks another agent. A crash in a parallel branch, while a node's
step is under way, re-runs the node: it joins the same step.
"""

# pylint: disable=missing-function-docstring,missing-class-docstring

import asyncio
import collections
import threading
import typing
import uuid
from typing import Any, Required, TypedDict

import httpx2
import pytest

pytest.importorskip("langgraph")

from langchain_core.runnables import RunnableConfig  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

import restate  # noqa: E402
from restate import HarnessEnvironment, sessions  # noqa: E402
from restate.agents import AgentObject  # noqa: E402
from restate.agents.langgraph import SqlCheckpointSaver, nodes  # noqa: E402


class State(TypedDict, total=False):
    refund: Required[str]
    approved: Any
    receipt: str
    findings: str


shipping = restate.Workflow("nodes_shipping")
shipped: collections.Counter[str] = collections.Counter()


@shipping.main()
async def ship(ctx: restate.WorkflowContext, order: str) -> str:
    shipped[order] += 1
    return f"shipped {order}"


researcher = restate.VirtualObject("nodes_researcher")
asked: collections.Counter[str] = collections.Counter()
researching = threading.Event()
crashed = threading.Event()


@researcher.handler()
async def chat(ctx: restate.ObjectContext, question: str) -> str:
    asked[question] += 1
    if question.startswith("slow"):
        # Answer only once the graph's attempt has crashed, so the crash cuts the node's call short.
        researching.set()
        while not crashed.is_set():
            await asyncio.sleep(0.01)
    return f"found: {question}"


def approval_then_shipping() -> Any:
    graph = StateGraph(State)
    graph.add_node("approve", nodes.wait_for_signal("approval", input=lambda s: s["refund"], output="approved"))
    graph.add_node("ship", nodes.workflow(ship, input=lambda s: s["refund"], output="receipt"))
    graph.add_node("research", nodes.subagent(researcher, message=lambda s: f"why {s['refund']}", output="findings"))
    graph.add_edge(START, "approve")
    graph.add_edge("approve", "ship")
    graph.add_edge("ship", "research")
    graph.add_edge("research", END)
    return graph


crashes: collections.Counter[str] = collections.Counter()
go_crash = threading.Event()


def crash_after(ready: threading.Event) -> Any:
    """A node that fails its first run once ``ready`` is set, while the node beside it is under way."""

    async def crash(state: State) -> dict[str, Any]:
        if crashes[state["refund"]] == 0:
            while not ready.is_set():
                await asyncio.sleep(0.01)
            crashes[state["refund"]] += 1
            crashed.set()
            raise RuntimeError("the process crashed")
        return {}

    return crash


def beside_a_crash(step: str) -> Any:
    """``step`` and a node that crashes while it is under way, at once."""
    graph = StateGraph(State)
    if step == "research":
        graph.add_node(step, nodes.subagent(researcher, message=lambda s: f"slow {s['refund']}", output="findings"))
        graph.add_node("crash", typing.cast(Any, crash_after(researching)))
    else:
        graph.add_node(step, nodes.wait_for_signal("approval", input=lambda s: s["refund"], output="approved"))
        graph.add_node("crash", typing.cast(Any, crash_after(go_crash)))
    graph.add_edge(START, step)
    graph.add_edge(START, "crash")
    graph.add_edge([step, "crash"], END)
    return graph


GRAPHS = {
    "approval": approval_then_shipping(),
    "research_crash": beside_a_crash("research"),
    "wait_crash": beside_a_crash("approve"),
}

desk = AgentObject("nodes_desk")


class Run(TypedDict):
    graph: str
    refund: str


@desk.handler()
async def run(ctx: sessions.SessionContext, request: Run) -> State:
    app = GRAPHS[request["graph"]].compile(checkpointer=SqlCheckpointSaver(ctx.db))
    config: RunnableConfig = {"configurable": {"thread_id": request["graph"]}}
    resuming = (await app.aget_state(config)).next
    start: State | None = None if resuming else {"refund": request["refund"]}
    return typing.cast(State, await app.ainvoke(start, config, context=ctx, durability="sync"))


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        restate.app([desk, shipping, researcher]), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


pytestmark = [pytest.mark.anyio]


async def post(harness: HarnessEnvironment, path: str, body: Any = None) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=60) as ingress:
        return await ingress.post(f"{harness.ingress_url}/nodes_desk/{path}", json=body)


async def pending(harness: HarnessEnvironment, key: str) -> list[dict[str, Any]]:
    return typing.cast(list[dict[str, Any]], (await post(harness, f"{key}/pending")).json())


async def until_waiting(harness: HarnessEnvironment, key: str) -> list[dict[str, Any]]:
    for _ in range(400):
        if waits := await pending(harness, key):
            return waits
        await asyncio.sleep(0.05)
    raise AssertionError("nothing waits")


async def test_a_graph_waits_for_a_signal_runs_a_workflow_and_asks_another_agent(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    running = asyncio.ensure_future(post(harness, f"{key}/run", {"graph": "approval", "refund": key}))
    waits = await until_waiting(harness, key)
    assert [(w["name"], w.get("input")) for w in waits] == [("approval", key)]
    resolved = await post(harness, f"{key}/signal", {"name": "approval", "value": {"by": "manager"}})
    assert resolved.json() == {"resolved": waits[0]["id"]}
    assert (await running).json() == {
        "refund": key,
        "approved": {"by": "manager"},
        "receipt": f"shipped {key}",
        "findings": f"found: why {key}",
    }
    assert shipped[key] == 1
    assert asked[f"why {key}"] == 1


async def test_a_node_cut_off_by_a_crash_joins_its_call_to_another_agent(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    state = (await post(harness, f"{key}/run", {"graph": "research_crash", "refund": key})).json()
    assert crashes[key] == 1
    assert state["findings"] == f"found: slow {key}"
    assert asked[f"slow {key}"] == 1


async def test_a_node_cut_off_by_a_crash_waits_for_the_same_signal(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    running = asyncio.ensure_future(post(harness, f"{key}/run", {"graph": "wait_crash", "refund": key}))
    first = await until_waiting(harness, key)
    go_crash.set()
    for _ in range(400):
        if crashes[key] == 1:
            break
        await asyncio.sleep(0.05)
    assert crashes[key] == 1
    await asyncio.sleep(0.5)  # the retried attempt waits again
    assert await pending(harness, key) == first
    await post(harness, f"{key}/signal", {"name": "approval", "value": "yes"})
    assert (await running).json()["approved"] == "yes"


def test_a_workflow_node_takes_input_exactly_when_its_handler_does():
    with pytest.raises(TypeError, match="takes input: pass input="):
        nodes.workflow(ship)
