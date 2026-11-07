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
import operator
import collections
import threading
import typing
import uuid
from typing import Annotated, Any, Required, TypedDict

import httpx2
import pytest

pytest.importorskip("langgraph")

from langchain_core.runnables import RunnableConfig  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Send  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import restate  # noqa: E402
from support import RESTATE_IMAGE  # noqa: E402  # pylint: disable=import-error
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


class Invoice(BaseModel):
    order: str
    amount: float


billing = restate.Workflow("nodes_billing")
billed: collections.Counter[str] = collections.Counter()


@billing.main()
async def bill(ctx: restate.WorkflowContext, invoice: Invoice) -> str:
    billed[invoice.order] += 1
    return f"billed ${invoice.amount:g} for {invoice.order}"


def billing_graph(amount: object) -> Any:
    """A workflow node whose input, from the state, is a dict: the workflow takes an ``Invoice``."""
    graph = StateGraph(State)
    node = nodes.workflow(bill, input=lambda s: {"order": s["refund"], "amount": amount}, output="receipt")
    graph.add_node("bill", node)
    graph.add_edge(START, "bill")
    graph.add_edge("bill", END)
    return graph


ledger = restate.VirtualObject("nodes_ledger")


@ledger.handler()
async def add(ctx: restate.ObjectContext, invoice: Invoice) -> str:
    total = (await ctx.get("total", type_hint=float) or 0) + invoice.amount
    ctx.set("total", total)
    return f"{ctx.key()} owes ${total:g}"


notices = restate.Service("nodes_notices")
noticed: collections.Counter[str] = collections.Counter()


@notices.handler()
async def notice(_ctx: restate.Context, invoice: Invoice) -> None:
    noticed[invoice.order] += 1


def call_then_send() -> Any:
    """A call to a virtual object, then a send to a service: both with a dict from the state as the input."""
    graph = StateGraph(State)
    invoice = typing.cast(Any, lambda s: {"order": s["refund"], "amount": 4})
    graph.add_node("add", nodes.call(add, input=invoice, key=lambda s: s["refund"], output="receipt"))
    graph.add_node("notice", nodes.send(notice, input=invoice))
    graph.add_edge(START, "add")
    graph.add_edge("add", "notice")
    graph.add_edge("notice", END)
    return graph


async def until(event: threading.Event) -> None:
    """Wait until ``event`` is set, for at most 20 seconds, so that a broken test fails rather than hangs."""
    for _ in range(2000):
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the event never came")


researcher = restate.VirtualObject("nodes_researcher")
asked: collections.Counter[str] = collections.Counter()
researching = threading.Event()
crashed = threading.Event()
deep_researching = threading.Event()
deep_crashed = threading.Event()


@researcher.handler()
async def chat(ctx: restate.ObjectContext, question: str) -> str:
    asked[question] += 1
    # Answer only once the graph's attempt has crashed, so the crash cuts the node's call short.
    if question.startswith("slow"):
        researching.set()
        await until(crashed)
    if question.startswith("deep"):
        deep_researching.set()
        await until(deep_crashed)
    return f"found: {question}"


def approval_then_shipping() -> Any:
    graph = StateGraph(State)
    # A model as what the graph waits for: pending shows it as JSON.
    waiting_for = typing.cast(Any, lambda s: Invoice(order=s["refund"], amount=1))
    graph.add_node("approve", nodes.wait_for_signal("approval", input=waiting_for, output="approved"))
    graph.add_node("ship", nodes.workflow(ship, input=lambda s: s["refund"], output="receipt"))
    graph.add_node("research", nodes.subagent(researcher, message=lambda s: f"why {s['refund']}", output="findings"))
    graph.add_edge(START, "approve")
    graph.add_edge("approve", "ship")
    graph.add_edge("ship", "research")
    graph.add_edge("research", END)
    return graph


crashes: collections.Counter[str] = collections.Counter()
go_crash = threading.Event()


def crash_after(ready: threading.Event, done: threading.Event | None = None) -> Any:
    """A node that fails its first run once ``ready`` is set, while the node beside it is under way."""

    async def crash(state: State) -> dict[str, Any]:
        if crashes[state["refund"]] == 0:
            await until(ready)
            crashes[state["refund"]] += 1
            (done or crashed).set()
            raise RuntimeError("the process crashed")
        return {}

    return crash


def in_a_subgraph() -> Any:
    """A subagent node inside a subgraph, beside a node of the parent graph that crashes while it is under way."""
    inner = StateGraph(State)
    inner.add_node("research", nodes.subagent(researcher, message=lambda s: f"deep {s['refund']}", output="findings"))
    inner.add_edge(START, "research")
    inner.add_edge("research", END)
    graph = StateGraph(State)
    graph.add_node("inner", inner.compile())
    graph.add_node("crash", typing.cast(Any, crash_after(deep_researching, deep_crashed)))
    graph.add_edge(START, "inner")
    graph.add_edge(START, "crash")
    graph.add_edge(["inner", "crash"], END)
    return graph


class Fan(TypedDict, total=False):
    refund: Required[str]
    findings: Annotated[list[str], operator.add]


def fanned_out() -> Any:
    """The same subagent node, three times at once, with Send: each its own step."""
    graph = StateGraph(Fan)

    async def research(item: dict[str, str], *, runtime: Any) -> dict[str, Any]:
        node = nodes.subagent(researcher, message=lambda s: s["question"], key=lambda s: s["question"], output="found")
        found = await node(item, runtime=runtime)
        return {"findings": [found["found"]]}

    graph.add_node("research", typing.cast(Any, research))
    questions = typing.cast(Any, lambda s: [Send("research", {"question": f"fan {s['refund']} {i}"}) for i in range(3)])
    graph.add_conditional_edges(START, questions, ["research"])
    graph.add_edge("research", END)
    return graph


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
    "invoice": billing_graph(12.5),
    "bad_invoice": billing_graph("a lot"),
    "call_send": call_then_send(),
    "subgraph": in_a_subgraph(),
    "fan": fanned_out(),
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
        restate.app([desk, shipping, billing, ledger, notices, researcher]),
        restate_image=RESTATE_IMAGE,
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
    assert [(w["name"], w.get("input")) for w in waits] == [("approval", {"order": key, "amount": 1.0})]
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


async def test_a_node_refuses_to_run_unless_each_step_is_stored_before_it_runs():
    from langgraph.checkpoint.memory import InMemorySaver  # pylint: disable=import-outside-toplevel

    app = approval_then_shipping().compile(checkpointer=InMemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": "t"}}
    for durability in ({}, {"durability": "async"}, {"durability": "exit"}):
        with pytest.raises(TypeError, match='durability="sync"'):
            await app.ainvoke({"refund": "r"}, config, context=object(), **durability)


async def test_a_workflow_node_validates_plain_data_from_the_state_into_the_handlers_input(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    state = (await post(harness, f"{key}/run", {"graph": "invoice", "refund": key})).json()
    assert state["receipt"] == f"billed $12.5 for {key}"


async def test_input_that_does_not_validate_fails_the_node_for_good(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    failed = await post(harness, f"{key}/run", {"graph": "bad_invoice", "refund": key})
    assert failed.status_code == 500
    assert "the input of nodes_billing.bill is not valid" in failed.text and "amount" in failed.text
    assert billed[key] == 0


async def test_a_graph_calls_an_object_and_sends_to_a_service(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    state = (await post(harness, f"{key}/run", {"graph": "call_send", "refund": key})).json()
    assert state["receipt"] == f"{key} owes $4"
    for _ in range(100):
        if noticed[key]:
            break
        await asyncio.sleep(0.05)
    assert noticed[key] == 1


def test_a_node_takes_a_handler_whose_input_pydantic_does_not_know():
    import msgspec  # pylint: disable=import-outside-toplevel

    class Parcel(msgspec.Struct):
        weight: float

    parcels = restate.Workflow("nodes_parcels")

    @parcels.main()
    async def weigh(_ctx: restate.WorkflowContext, parcel: Parcel) -> float:
        return parcel.weight

    nodes.workflow(weigh, input=lambda s: {"weight": 1})  # the handler's serde takes the value as it is


async def test_a_node_in_a_subgraph_cut_off_by_a_crash_joins_its_step(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    state = (await post(harness, f"{key}/run", {"graph": "subgraph", "refund": key})).json()
    assert crashes[key] == 1
    assert state["findings"] == f"found: deep {key}"
    assert asked[f"deep {key}"] == 1


async def test_a_node_sent_to_at_once_makes_a_step_for_each(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    state = (await post(harness, f"{key}/run", {"graph": "fan", "refund": key})).json()
    questions = [f"fan {key} {i}" for i in range(3)]
    assert sorted(state["findings"]) == sorted(f"found: {q}" for q in questions)
    assert [asked[q] for q in questions] == [1, 1, 1]


def test_a_call_takes_a_key_exactly_when_its_handler_is_keyed():
    with pytest.raises(TypeError, match="is keyed: pass key="):
        nodes.call(add, input=lambda s: s)
    with pytest.raises(TypeError, match="is not keyed: drop key="):
        nodes.send(notice, input=lambda s: s, key=lambda s: "k")


def test_a_workflow_node_takes_input_exactly_when_its_handler_does():
    with pytest.raises(TypeError, match="takes input: pass input="):
        nodes.workflow(ship)
