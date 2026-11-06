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
"""``langgraph.agent(name, graph=...)``: chat runs a graph of your own, against a real Restate server.

The graph is scripted: it routes on the first word of the message.
"""

# pylint: disable=missing-function-docstring,missing-class-docstring

import asyncio
import collections
import threading
import typing
import uuid
from datetime import timedelta
from typing import Any, NotRequired, TypedDict

import httpx2
import pytest

pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage, ToolMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.prebuilt import ToolNode  # noqa: E402
from langgraph.types import interrupt  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import restate  # noqa: E402
from restate import HarnessEnvironment, sessions  # noqa: E402
from restate.agents import langgraph  # noqa: E402
from restate.agents.internal import CANCELLED  # noqa: E402
from restate.agents.langgraph import nodes  # noqa: E402


class Desk(MessagesState):
    approval: NotRequired[str]


def _asked(state: Desk) -> tuple[str, str]:
    """The first word of the message the run answers, and the rest."""
    last = next(m for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
    word, _, rest = str(typing.cast(BaseMessage, last).text).partition(" ")
    return word, rest


def route(state: Desk) -> str | list[str]:
    word, _ = _asked(state)
    routes = {"refund": "approve", "stop": "approve", "crash": "first", "plan": "plan", "tool": "call_tool"}
    routes |= {"history": "history", "trim": "trim", "humans": "humans", "ask": "ask", "twice": "twice"}
    routes |= {"deep": "deep"}
    if word == "both":
        return ["ask", "side"]  # two branches at once
    return routes.get(word, "greet")


def greet(state: Desk) -> dict[str, Any]:
    return {"messages": [AIMessage(f"hello {_asked(state)[1]}")]}


def confirm(state: Desk) -> dict[str, Any]:
    return {"messages": [AIMessage(f"approved: {state.get('approval')}")]}


firsts: collections.Counter[str] = collections.Counter()
crashes: collections.Counter[str] = collections.Counter()


def first(state: Desk) -> dict[str, Any]:
    rest = _asked(state)[1]
    firsts[rest] += 1
    return {"messages": [AIMessage(f"working on {rest}")]}


def second(state: Desk) -> dict[str, Any]:
    rest = _asked(state)[1]
    crashes[rest] += 1
    if crashes[rest] == 1:
        raise RuntimeError("the process crashed")
    return {"messages": [AIMessage(f"done {rest}")]}


tool_started = threading.Event()


@tool
async def blocking() -> str:
    """Runs until the turn is stopped."""
    tool_started.set()
    await asyncio.sleep(300)
    return "never"


def call_tool(state: Desk) -> dict[str, Any]:
    call = {"id": f"call-{_asked(state)[1]}", "name": "blocking", "args": {}}
    return {"messages": [AIMessage("", tool_calls=[call])]}


def history(state: Desk) -> dict[str, Any]:
    """What the graph's state holds besides the messages it was sent: AI messages and tool results."""
    seen = [
        f"tool:{m.tool_call_id}:{m.text}" if isinstance(m, ToolMessage) else f"ai:{m.text}"
        for m in state["messages"]
        if isinstance(m, AIMessage | ToolMessage) and (m.text or isinstance(m, ToolMessage))
    ]
    return {"messages": [AIMessage(" | ".join(seen))]}


def trim(state: Desk) -> dict[str, Any]:
    """Trim the history to the answer alone, as a summarizing graph does."""
    removed = [RemoveMessage(id=m.id) for m in state["messages"] if m.id is not None]
    return {"messages": [*removed, AIMessage("trimmed")]}


def humans(state: Desk) -> dict[str, Any]:
    """The messages the graph has from the user."""
    said = [str(m.text) for m in state["messages"] if isinstance(m, HumanMessage)]
    return {"messages": [AIMessage(" | ".join(said))]}


asks: collections.Counter[str] = collections.Counter()


def asking(state: Desk) -> dict[str, Any]:
    asks[_asked(state)[1]] += 1
    answer = interrupt({"approve": _asked(state)[1]})
    return {"messages": [AIMessage(f"asked: {answer}")]}


def twice(state: Desk) -> dict[str, Any]:
    first = interrupt("first?")
    second = interrupt("second?")
    return {"messages": [AIMessage(f"twice: {first} {second}")]}


def side(state: Desk) -> dict[str, Any]:
    return {"messages": [AIMessage(f"side: {interrupt('side?')}")]}


def plan(state: Desk) -> dict[str, Any]:
    return {"messages": [AIMessage(f'{{"title": "{_asked(state)[1]}"}}')]}


builder = StateGraph(Desk)
steps = {"greet": greet, "confirm": confirm, "first": first, "second": second, "plan": plan, "history": history}
steps |= {"trim": trim, "humans": humans, "ask": asking, "twice": twice, "side": side}
deep = StateGraph(Desk)  # a subgraph whose node interrupts twice
deep.add_node("twice", typing.cast(Any, twice))
deep.add_edge(START, "twice")
deep.add_edge("twice", END)
builder_nodes: dict[str, Any] = {**steps, "deep": deep.compile()}
for name, node in {**builder_nodes, "call_tool": call_tool}.items():
    builder.add_node(name, typing.cast(Any, node))
builder.add_node("tools", ToolNode([blocking]))
builder.add_node("approve", nodes.wait_for_signal("approval", output="approval"))
branches = [
    "approve",
    "first",
    "plan",
    "greet",
    "history",
    "call_tool",
    "trim",
    "humans",
    "ask",
    "twice",
    "side",
    "deep",
]
builder.add_conditional_edges(START, route, branches)
builder.add_edge("call_tool", "tools")
builder.add_edge("approve", "confirm")
builder.add_edge("first", "second")
for last in (
    "greet",
    "confirm",
    "second",
    "plan",
    "history",
    "tools",
    "trim",
    "humans",
    "ask",
    "twice",
    "side",
    "deep",
):
    builder.add_edge(last, END)


class Plan(BaseModel):
    title: str


desk = langgraph.agent("graph_desk", graph=builder)


@desk.handler()
async def ask(ctx: sessions.SessionContext, text: str) -> str:
    """A handler of your own that prompts the graph, as chat does."""
    return await langgraph.prompt(ctx, text, graph=builder)


compiled = langgraph.agent("graph_compiled", graph=builder.compile())
planner = langgraph.agent("graph_planner", graph=builder, output=Plan)
# Restate suspends its turns after a second of waiting.
patient = langgraph.agent("graph_patient", graph=builder, inactivity_timeout=timedelta(seconds=1))


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        restate.app([desk, compiled, planner, patient]), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


pytestmark = [pytest.mark.anyio]


async def post(harness: HarnessEnvironment, path: str, **kwargs: Any) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=60) as ingress:
        return await ingress.post(f"{harness.ingress_url}/{path}", **kwargs)


async def chat(harness: HarnessEnvironment, agent: str, key: str, text: str) -> httpx2.Response:
    return await post(harness, f"{agent}/{key}/chat", content=text, headers={"content-type": "text/plain"})


async def messages(harness: HarnessEnvironment, agent: str, key: str) -> list[tuple[str, str]]:
    page = (await post(harness, f"{agent}/{key}/messages", json={})).json()
    return [(m["role"], " ".join(str(p.get("text", "")) for p in m["parts"])) for m in page["messages"]]


async def until_waiting(harness: HarnessEnvironment, key: str, agent: str = "graph_desk") -> list[dict[str, Any]]:
    for _ in range(200):
        if waits := (await post(harness, f"{agent}/{key}/pending")).json():
            return typing.cast(list[dict[str, Any]], waits)
        await asyncio.sleep(0.05)
    raise AssertionError("nothing waits")


@pytest.mark.parametrize("agent", ["graph_desk", "graph_compiled"])
async def test_chat_runs_the_graph_and_the_conversation_goes_on(harness: HarnessEnvironment, agent: str):
    key = uuid.uuid4().hex
    assert (await chat(harness, agent, key, f"hi {key}")).json() == f"hello {key}"
    assert (await chat(harness, agent, key, "hi again")).json() == "hello again"
    assert await messages(harness, agent, key) == [
        ("user", f"hi {key}"),
        ("assistant", f"hello {key}"),
        ("user", "hi again"),
        ("assistant", "hello again"),
    ]


async def test_a_prebuilt_node_waits_for_a_signal_in_chat(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    answering = asyncio.ensure_future(chat(harness, "graph_desk", key, f"refund {key}"))
    [wait] = await until_waiting(harness, key)
    assert wait["name"] == "approval"
    await post(harness, f"graph_desk/{key}/signal", json={"name": "approval", "value": "yes"})
    assert (await answering).json() == "approved: yes"


async def test_a_crash_resumes_the_run_and_its_messages_are_copied_once(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    assert (await chat(harness, "graph_desk", key, f"crash {key}")).json() == f"done {key}"
    assert (firsts[key], crashes[key]) == (1, 2)
    assert await messages(harness, "graph_desk", key) == [
        ("user", f"crash {key}"),
        ("assistant", f"working on {key}"),
        ("assistant", f"done {key}"),
    ]


async def test_after_a_stopped_run_the_next_message_starts_a_run_of_its_own(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    answering = asyncio.ensure_future(chat(harness, "graph_desk", key, f"stop {key}"))
    await until_waiting(harness, key)
    await post(harness, f"graph_desk/{key}/stop")
    assert (await answering).status_code == 409
    assert (await post(harness, f"graph_desk/{key}/pending")).json() == []
    assert (await chat(harness, "graph_desk", key, f"hi {key}")).json() == f"hello {key}"


async def test_the_graph_sees_how_a_stopped_run_ended(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    answering = asyncio.ensure_future(chat(harness, "graph_desk", key, f"stop {key}"))
    await until_waiting(harness, key)
    await post(harness, f"graph_desk/{key}/stop")
    assert (await answering).status_code == 409
    assert (await chat(harness, "graph_desk", key, "history")).json() == f"ai:{CANCELLED}"


async def test_a_tool_call_cut_off_by_a_stop_gets_a_result_in_the_graph(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    tool_started.clear()
    answering = asyncio.ensure_future(chat(harness, "graph_desk", key, f"tool {key}"))
    for _ in range(200):
        if tool_started.is_set():
            break
        await asyncio.sleep(0.05)
    await post(harness, f"graph_desk/{key}/stop")
    assert (await answering).status_code == 409
    # The graph's history has no tool call without a result, which a model provider would refuse.
    expected = f"tool:call-{key}:{CANCELLED} | ai:{CANCELLED}"
    assert (await chat(harness, "graph_desk", key, "history")).json() == expected
    # Carried once: the next run does not add them again.
    assert (await chat(harness, "graph_desk", key, "history")).json() == f"{expected} | ai:{expected}"


async def test_a_handler_of_your_own_prompts_the_graph(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    assert (await post(harness, f"graph_desk/{key}/ask", json=f"hi {key}")).json() == f"hello {key}"
    assert (await chat(harness, "graph_desk", key, "hi again")).json() == "hello again"
    assert await messages(harness, "graph_desk", key) == [
        ("user", f"hi {key}"),
        ("assistant", f"hello {key}"),
        ("user", "hi again"),
        ("assistant", "hello again"),
    ]


async def test_a_message_the_graph_trimmed_is_not_brought_back(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    assert (await chat(harness, "graph_desk", key, f"trim {key}")).json() == "trimmed"
    assert (await chat(harness, "graph_desk", key, "humans")).json() == "humans"


async def test_an_interrupt_waits_for_a_signal_and_resumes_with_its_value(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    answering = asyncio.ensure_future(chat(harness, "graph_desk", key, f"ask {key}"))
    [wait] = await until_waiting(harness, key)
    assert (wait["name"], wait["input"]) == ("interrupt", {"approve": key})
    await post(harness, f"graph_desk/{key}/signal", json={"name": "interrupt", "value": "yes"})
    assert (await answering).json() == "asked: yes"
    assert (await post(harness, f"graph_desk/{key}/pending")).json() == []


@pytest.mark.parametrize("word", ["twice", "deep"])
async def test_a_node_that_interrupts_twice_waits_twice(harness: HarnessEnvironment, word: str):
    """In the graph, and in a subgraph, whose resume values are in its own checkpoint."""
    key = uuid.uuid4().hex
    answering = asyncio.ensure_future(chat(harness, "graph_desk", key, word))
    [first] = await until_waiting(harness, key)
    assert first["input"] == "first?"
    await post(harness, f"graph_desk/{key}/signal", json={"id": first["id"], "value": "A"})
    second = first
    for _ in range(200):
        waits = (await post(harness, f"graph_desk/{key}/pending")).json()
        if waits and waits[0]["id"] != first["id"]:
            [second] = waits
            break
        await asyncio.sleep(0.05)
    assert second["input"] == "second?"
    await post(harness, f"graph_desk/{key}/signal", json={"id": second["id"], "value": "B"})
    assert (await answering).json() == "twice: A B"


async def test_interrupts_of_branches_at_once_wait_at_once(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    answering = asyncio.ensure_future(chat(harness, "graph_desk", key, f"both {key}"))
    waits: list[dict[str, Any]] = []
    for _ in range(200):
        if len(waits := (await post(harness, f"graph_desk/{key}/pending")).json()) == 2:
            break
        await asyncio.sleep(0.05)
    assert sorted(str(w["input"]) for w in waits) == sorted([str({"approve": key}), "side?"])
    for wait in waits:
        await post(harness, f"graph_desk/{key}/signal", json={"id": wait["id"], "value": "ok"})
    await answering
    shown = await messages(harness, "graph_desk", key)
    assert sorted(text for role, text in shown if role == "assistant") == ["asked: ok", "side: ok"]


@pytest.mark.parametrize("word", ["ask", "refund"])
async def test_a_killed_turn_leaves_no_wait_behind(harness: HarnessEnvironment, word: str):
    """An interrupt's wait, and a wait_for_signal node's, end with their turn, as the companion closes them."""
    key = uuid.uuid4().hex
    answering = asyncio.ensure_future(chat(harness, "graph_desk", key, f"{word} {key}"))
    await until_waiting(harness, key)
    turn = (await post(harness, f"graph_desk/{key}/status")).json()["answering"]["invocationId"]
    async with httpx2.AsyncClient() as admin:
        killed = await admin.patch(f"{harness.admin_api_url}/invocations/{turn}/kill")
    assert killed.status_code < 300, killed.text
    await answering
    for _ in range(200):
        if (await post(harness, f"graph_desk/{key}/pending")).json() == []:
            break
        await asyncio.sleep(0.05)
    assert (await post(harness, f"graph_desk/{key}/pending")).json() == []


async def test_a_turn_suspended_while_it_waits_resumes_on_the_same_wait(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    answering = asyncio.ensure_future(chat(harness, "graph_patient", key, f"ask {key}"))
    [wait] = await until_waiting(harness, key, "graph_patient")
    await asyncio.sleep(2.5)  # Restate suspends the turn
    query = (
        "SELECT status FROM sys_invocation WHERE target_service_name = 'graph_patient' AND target_handler_name = 'chat'"
    )
    async with httpx2.AsyncClient() as admin:
        found = await admin.post(
            f"{harness.admin_api_url}/query", json={"query": query}, headers={"accept": "application/json"}
        )
    assert [row["status"] for row in found.json()["rows"]] == ["suspended"]
    assert (await post(harness, f"graph_patient/{key}/pending")).json() == [wait]
    await post(harness, f"graph_patient/{key}/signal", json={"id": wait["id"], "value": "later"})
    assert (await answering).json() == "asked: later"


async def test_output_validates_the_last_ai_message(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    assert (await chat(harness, "graph_planner", key, f"plan {key}")).json() == {"title": key}


async def test_langgraph_streams_a_step_only_once_its_checkpoint_is_stored():
    """What copying messages relies on: with durability="sync", a streamed state is stored, even by a slow saver."""
    from langgraph.checkpoint.memory import InMemorySaver  # pylint: disable=import-outside-toplevel

    class Slow(InMemorySaver):
        async def aput(self, *args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(0.1)
            return await super().aput(*args, **kwargs)

    saver = Slow()
    app: Any = builder.compile(checkpointer=saver)
    config: Any = {"configurable": {"thread_id": "t"}}
    states = app.astream({"messages": [HumanMessage("hi slow")]}, config, durability="sync", stream_mode="values")
    async for state in states:
        stored = await saver.aget_tuple(config)
        assert stored is not None
        assert len(stored.checkpoint["channel_values"]["messages"]) == len(state["messages"])


def test_a_graph_has_its_own_model_and_its_state_has_messages():
    with pytest.raises(TypeError, match="pass no model, system or tools"):
        langgraph.agent("both", graph=builder, system="Hi.")

    class Plain(TypedDict):
        count: int

    with pytest.raises(TypeError, match="MessagesState"):
        langgraph.agent("plain", graph=StateGraph(Plain))
