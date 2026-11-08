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
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, NotRequired, TypedDict

import httpx2
import pytest

pytest.importorskip("langgraph")

from langchain_core.callbacks import CallbackManagerForLLMRun  # noqa: E402
from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langgraph.errors import InvalidUpdateError  # noqa: E402
from langgraph.graph import END, START, MessagesState, StateGraph  # noqa: E402
from langgraph.prebuilt import ToolNode  # noqa: E402
from langgraph.runtime import Runtime  # noqa: E402
from langgraph.types import interrupt  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import restate  # noqa: E402
from support import RESTATE_IMAGE  # noqa: E402  # pylint: disable=import-error
from restate import HarnessEnvironment, sessions  # noqa: E402
from restate.agents import langgraph  # noqa: E402
from restate.agents.internal import CANCELLED, ENDED  # noqa: E402
from restate.agents.langgraph import nodes  # noqa: E402
from restate.agents.langgraph._errors import terminal_of  # noqa: E402
from restate.agents.langgraph._graph import at_breakpoint  # noqa: E402
from restate.exceptions import TerminalError  # noqa: E402


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
    routes |= {"deep": "deep", "pause": "pause", "again": "again", "lookup": "call_lookup", "fetch": "fetch"}
    routes |= {"consult": "call_lookup", "count": "call_lookup", "refuse": "refuse", "nested": "nested"}
    routes |= {"give_up": "give_up", "stops": "stops", "loop": "loop", "forget": "forget"}
    if word == "both":
        return ["ask", "side"]  # two branches at once
    if word == "clash":
        return ["clash_a", "clash_b"]  # two branches at once, each writing the approval
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


def forget(state: Desk) -> dict[str, Any]:
    """Keep the latest prompt and what followed it, as the graph that trims its state in ``_graph.py`` does."""
    prompts = [i for i, m in enumerate(state["messages"]) if isinstance(m, HumanMessage)]
    old = state["messages"][: prompts[-1]] if len(prompts) > 1 else []
    return {"messages": [*(RemoveMessage(id=typing.cast(str, m.id)) for m in old), AIMessage("forgot")]}


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
steps |= {"trim": trim, "forget": forget, "humans": humans, "ask": asking, "twice": twice, "side": side}
deep = StateGraph(Desk)  # a subgraph whose node interrupts twice
deep.add_node("twice", typing.cast(Any, twice))
deep.add_edge(START, "twice")
deep.add_edge("twice", END)
paused = StateGraph(Desk)  # a subgraph that greets, compiled with a static breakpoint where a test needs one
paused.add_node("greet", typing.cast(Any, greet))
paused.add_edge(START, "greet")
paused.add_edge("greet", END)


async def pause(state: Desk) -> dict[str, Any]:
    """Run a subgraph compiled here, which stops at a static breakpoint."""
    subgraph: Any = paused.compile(interrupt_before=["greet"])
    return await subgraph.ainvoke(state)


greeting: Any = paused.compile()  # Any: LangGraph types a compiled graph's input loosely
asked_twice: Any = deep.compile()


async def nested(state: Desk) -> dict[str, Any]:
    """Ask for an approval itself, then run a subgraph whose node interrupts twice: LangGraph lists each of those
    interrupts again on this node's task, with this node's own resume values."""
    approved = interrupt("approve?")
    inner = await asked_twice.ainvoke(state)
    return {"messages": [AIMessage(f"{approved}: {inner['messages'][-1].text}")]}


async def again(state: Desk) -> dict[str, Any]:
    """Call a subgraph, then one whose node interrupts twice: the second's state is in namespace again:<task>|1."""
    await greeting.ainvoke(state)
    return await asked_twice.ainvoke(state)


class AuthenticationError(Exception):
    """As a model SDK names the error of a wrong API key."""


class NotFoundError(Exception):
    """As an SDK names a missing resource: the model's provider refusing, if its model raises it."""


lookups: collections.Counter[str] = collections.Counter()
fetches: collections.Counter[str] = collections.Counter()


@tool
async def lookup(order: str) -> str:
    """An order's status, from an API whose first answer is an error, as if it were briefly down."""
    lookups[order] += 1
    if lookups[order] == 1:
        raise NotFoundError(f"order {order} not found")
    return f"order {order} shipped"


consults: collections.Counter[str] = collections.Counter()


@tool
async def consult(order: str) -> str:
    """A second opinion from a model, whose first request its provider refuses: the tool's error, not the agent's."""
    consults[order] += 1
    if consults[order] == 1:
        await Refusing().ainvoke(order)
    return f"order {order} looks fine"


counts: collections.Counter[str] = collections.Counter()


@tool
async def count(order: str) -> str:
    """How many items an order has, from a cache whose first lookup misses: a KeyError, of a built-in type."""
    counts[order] += 1
    if counts[order] == 1:
        raise KeyError(order)
    return f"order {order} has 3 items"


def call_lookup(state: Desk) -> dict[str, Any]:
    """Call the tool the message names (lookup, consult or count) for the order it names."""
    name, order = _asked(state)
    return {"messages": [AIMessage("", tool_calls=[{"id": f"{name}-{order}", "name": name, "args": {"order": order}}])]}


def found(state: Desk) -> dict[str, Any]:
    result = next(m for m in reversed(state["messages"]) if isinstance(m, ToolMessage))
    return {"messages": [AIMessage(f"found: {result.text}")]}


def fetch(state: Desk) -> dict[str, Any]:
    """A node whose first run fails with an error named as a model provider's refusal."""
    rest = _asked(state)[1]
    fetches[rest] += 1
    if fetches[rest] == 1:
        raise NotFoundError(f"{rest} not found")
    return {"messages": [AIMessage(f"fetched {rest}")]}


refusals: collections.Counter[str] = collections.Counter()


class Refusing(BaseChatModel):
    """A model whose provider refuses the first request of each text, as for a wrong key, and then answers."""

    @property
    def _llm_type(self) -> str:
        return "refusing"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        text = str(messages[-1].text)
        refusals[text] += 1
        if refusals[text] == 1:
            raise AuthenticationError("invalid x-api-key")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(f"answered {text}"))])


async def refuse(state: Desk) -> dict[str, Any]:
    """The graph's own model, whose provider refuses its first request."""
    return {"messages": [await Refusing().ainvoke(state["messages"])]}


def give_up(state: Desk) -> dict[str, Any]:
    """A node that ends the turn for good."""
    raise TerminalError(f"no refunds for {_asked(state)[1]}")


loops: collections.Counter[str] = collections.Counter()


def loop(state: Desk) -> dict[str, Any]:
    """A node that runs again and again, as a model that keeps calling tools does, until the recursion limit."""
    loops[_asked(state)[1]] += 1
    return {}


def clash_a(state: Desk) -> dict[str, Any]:
    return {"approval": "a"}


def clash_b(state: Desk) -> dict[str, Any]:
    """Writes the approval in the same step as clash_a: a field that takes one value per step."""
    return {"approval": "b"}


builder_nodes: dict[str, Any] = {**steps, "deep": deep.compile(), "pause": pause, "again": again}
builder_nodes |= {"call_lookup": call_lookup, "found": found, "fetch": fetch, "refuse": refuse}
# A subgraph node compiled with a static breakpoint: LangGraph's public API does not list subgraphs, so it is
# found when it stops a run.
builder_nodes |= {"nested": nested, "give_up": give_up, "stops": paused.compile(interrupt_before=["greet"])}
builder_nodes |= {"loop": loop, "clash_a": clash_a, "clash_b": clash_b}
for name, node in {**builder_nodes, "call_tool": call_tool}.items():
    builder.add_node(name, typing.cast(Any, node))
builder.add_node("tools", ToolNode([blocking]))
builder.add_node("lookup_tools", ToolNode([lookup, consult, count]))
builder.add_node("approve", nodes.wait_for_signal("approval", output="approval"))
branches = [
    "approve",
    "first",
    "plan",
    "greet",
    "history",
    "call_tool",
    "trim",
    "forget",
    "humans",
    "ask",
    "twice",
    "side",
    "deep",
    "pause",
    "again",
    "call_lookup",
    "fetch",
    "refuse",
    "nested",
    "give_up",
    "stops",
    "loop",
    "clash_a",
    "clash_b",
]
builder.add_conditional_edges(START, route, branches)
builder.add_edge("call_tool", "tools")
builder.add_edge("call_lookup", "lookup_tools")
builder.add_edge("lookup_tools", "found")
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
    "forget",
    "humans",
    "ask",
    "twice",
    "side",
    "deep",
    "pause",
    "again",
    "found",
    "fetch",
    "refuse",
    "nested",
    "give_up",
    "stops",
    "clash_a",
    "clash_b",
):
    builder.add_edge(last, END)
builder.add_edge("loop", "loop")


class Plan(BaseModel):
    title: str


desk = langgraph.agent("graph_desk", graph=builder)


@desk.handler()
async def ask(ctx: sessions.SessionContext, text: str) -> str:
    """A handler of your own that prompts the graph, as chat does."""
    return await langgraph.prompt(ctx, text, graph=builder)


@desk.handler()
async def checkpoints(ctx: sessions.SessionContext) -> dict[str, int]:
    """How many checkpoints the key's database has, by namespace."""
    rows = await ctx.db.values(
        "SELECT checkpoint_ns, count(*) FROM restate_langgraph_checkpoints GROUP BY checkpoint_ns"
    )
    return {str(ns): int(typing.cast(int, n)) for ns, n in rows}


@desk.handler()
async def holding(ctx: sessions.SessionContext, text: str) -> list[str]:
    """The checkpointer's tables that hold ``text`` in any of their values."""
    columns = {
        "restate_langgraph_checkpoints": "checkpoint, metadata",
        "restate_langgraph_channel_values": "value",
        "restate_langgraph_writes": "value",
    }
    found: list[str] = []
    for table, selected in columns.items():
        rows = await ctx.db.values(f"SELECT {selected} FROM {table}")
        stored = [value if isinstance(value, bytes) else str(value).encode() for row in rows for value in row]
        if any(text.encode() in value for value in stored):
            found.append(table)
    return found


compiled = langgraph.agent("graph_compiled", graph=builder.compile())
limited = langgraph.agent("graph_limited", graph=builder.compile().with_config(recursion_limit=10))
planner = langgraph.agent("graph_planner", graph=builder, output=Plan)
# Restate suspends its turns after a second of waiting.
patient = langgraph.agent("graph_patient", graph=builder, inactivity_timeout=timedelta(seconds=1))


@dataclass
class Caller:
    """The runtime context of a graph that has one of its own."""

    name: str


def whoami(state: Desk, runtime: Runtime[Caller]) -> dict[str, Any]:
    return {"messages": [AIMessage(f"you are {runtime.context.name}")]}


contextual = StateGraph(Desk, context_schema=Caller)
contextual.add_node("whoami", typing.cast(Any, whoami))
contextual.add_node("approve", nodes.wait_for_signal("approval", output="approval"))
contextual.add_node("confirm", typing.cast(Any, confirm))
contextual.add_conditional_edges(
    START, lambda state: "approve" if _asked(state)[0] == "refund" else "whoami", ["approve", "whoami"]
)
contextual.add_edge("approve", "confirm")
contextual.add_edge("confirm", END)
contextual.add_edge("whoami", END)
caller = langgraph.agent("graph_context", graph=contextual, context=lambda ctx: Caller(ctx.key))


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        restate.app([desk, compiled, limited, planner, patient, caller]),
        restate_image=RESTATE_IMAGE,
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
    # The graph's state holds every turn; the conversation loads only the latest whole, and copies each message once.
    assert (await chat(harness, agent, key, "hi there")).json() == "hello there"
    assert await messages(harness, agent, key) == [
        ("user", f"hi {key}"),
        ("assistant", f"hello {key}"),
        ("user", "hi again"),
        ("assistant", "hello again"),
        ("user", "hi there"),
        ("assistant", "hello there"),
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


async def test_a_message_the_graph_removed_is_gone_from_its_checkpoints_once_the_turn_ends(
    harness: HarnessEnvironment,
):
    """The graph's state is its own to trim: once a run removes a message, the turn's prune deletes what held it."""
    key = uuid.uuid4().hex
    assert (await chat(harness, "graph_desk", key, f"hi {key}")).json() == f"hello {key}"
    assert "restate_langgraph_channel_values" in (await post(harness, f"graph_desk/{key}/holding", json=key)).json()
    assert (await chat(harness, "graph_desk", key, "forget")).json() == "forgot"
    assert (await post(harness, f"graph_desk/{key}/holding", json=key)).json() == []
    # The conversation is a log: it keeps the messages the graph removed.
    assert (await messages(harness, "graph_desk", key))[:2] == [("user", f"hi {key}"), ("assistant", f"hello {key}")]


async def test_an_interrupt_waits_for_a_signal_and_resumes_with_its_value(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    answering = asyncio.ensure_future(chat(harness, "graph_desk", key, f"ask {key}"))
    [wait] = await until_waiting(harness, key)
    assert (wait["name"], wait["input"]) == ("interrupt", {"approve": key})
    await post(harness, f"graph_desk/{key}/signal", json={"name": "interrupt", "value": "yes"})
    assert (await answering).json() == "asked: yes"
    assert (await post(harness, f"graph_desk/{key}/pending")).json() == []


@pytest.mark.parametrize("word", ["twice", "deep", "again"])
async def test_a_node_that_interrupts_twice_waits_twice(harness: HarnessEnvironment, word: str):
    """In the graph, in a subgraph, whose resume values are in its own checkpoint, and in a node's second subgraph
    call, whose state LangGraph's subgraph snapshots do not show."""
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


async def test_each_interrupt_below_a_node_that_interrupts_itself_waits_for_its_own_signal(
    harness: HarnessEnvironment,
):
    """A node asks for an approval, then runs a subgraph that interrupts twice: three waits, one after another."""
    key = uuid.uuid4().hex
    answering = asyncio.ensure_future(chat(harness, "graph_desk", key, "nested"))
    answers = {"approve?": "yes", "first?": "A", "second?": "B"}
    asked: dict[str, str] = {}
    for _ in range(400):
        if answering.done():
            break
        for wait in (await post(harness, f"graph_desk/{key}/pending")).json():
            if wait["id"] not in asked:
                asked[wait["id"]] = wait["input"]
                await post(
                    harness, f"graph_desk/{key}/signal", json={"id": wait["id"], "value": answers[wait["input"]]}
                )
        await asyncio.sleep(0.05)
    assert (await answering).json() == "yes: twice: A B"
    assert list(asked.values()) == ["approve?", "first?", "second?"]


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
    # The companion has the agent close the killed turn in the conversation, before any next message.
    for _ in range(200):
        if (await messages(harness, "graph_desk", key))[-1] == ("assistant", ENDED):
            break
        await asyncio.sleep(0.05)
    assert (await messages(harness, "graph_desk", key))[-1] == ("assistant", ENDED)


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


@pytest.mark.parametrize("word", ["pause", "stops"])
async def test_a_static_breakpoint_in_a_subgraph_fails_the_turn_and_the_next_message_starts_afresh(
    harness: HarnessEnvironment, word: str
):
    """A static breakpoint pauses the run with nothing to wait for: the turn fails rather than answer half done. The
    subgraph is one that a node calls, or a node itself."""
    key = uuid.uuid4().hex
    failed = await chat(harness, "graph_desk", key, f"{word} {key}")
    assert failed.status_code == 500 and "interrupt()" in failed.text, failed.text
    assert (await chat(harness, "graph_desk", key, f"hi {key}")).json() == f"hello {key}"


@pytest.mark.parametrize("word", ["lookup", "fetch", "consult", "count", "refuse"])
async def test_any_error_of_the_graph_is_retried_whatever_its_name(harness: HarnessEnvironment, word: str):
    """Named as a model provider's refusal, it is no refusal: a tool's, a node's, a model's that a tool calls, an
    error of a built-in type (count's KeyError), or the graph's own model's."""
    key = uuid.uuid4().hex
    answered = await chat(harness, "graph_desk", key, f"{word} {key}")
    assert answered.status_code == 200, answered.text
    expected = {
        "lookup": f"found: order {key} shipped",
        "fetch": f"fetched {key}",
        "consult": f"found: order {key} looks fine",
        "count": f"found: order {key} has 3 items",
        "refuse": f"answered refuse {key}",
    }
    assert answered.json() == expected[word]
    tries = {"lookup": lookups, "fetch": fetches, "consult": consults, "count": counts, "refuse": refusals}[word]
    assert tries[f"refuse {key}" if word == "refuse" else key] == 2


async def test_a_terminal_error_of_a_node_ends_the_turn_and_the_next_is_answered(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    failed = await chat(harness, "graph_desk", key, f"give_up {key}")
    assert failed.status_code == 500 and f"no refunds for {key}" in failed.text, failed.text
    assert (await chat(harness, "graph_desk", key, f"hi {key}")).json() == f"hello {key}"


async def test_a_graph_past_its_recursion_limit_ends_the_turn_and_runs_no_further(harness: HarnessEnvironment):
    """A retry would resume the run with a fresh limit, and run on: the turn ends for good, after one attempt."""
    key = uuid.uuid4().hex
    failed = await chat(harness, "graph_limited", key, f"loop {key}")
    assert failed.status_code == 500 and "recursion limit" in failed.text, failed.text
    assert loops[key] == 10  # the graph's recursion_limit: one attempt's steps
    # The next message starts a run of its own: the loop does not go on.
    assert (await chat(harness, "graph_limited", key, f"hi {key}")).json() == f"hello {key}"
    assert loops[key] == 10


async def test_an_invalid_update_of_the_graph_ends_the_turn(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    failed = await chat(harness, "graph_desk", key, f"clash {key}")
    assert failed.status_code == 500 and "invalid state update" in failed.text, failed.text
    # The writes that do not fit stay in the checkpoint, where LangGraph cannot show its state with them: the next
    # message starts a run of its own all the same.
    assert (await chat(harness, "graph_desk", key, f"hi {key}")).json() == f"hello {key}"


async def test_a_run_resumed_after_an_invalid_update_ends_its_turn(
    harness: HarnessEnvironment, monkeypatch: pytest.MonkeyPatch
):
    """As after a crash before the turn ended, or on a key that an earlier version retried: the update's writes are
    stored, the retried attempt resumes the run that made them, and LangGraph cannot show its state with them."""
    from restate.agents.langgraph import _graph  # pylint: disable=import-outside-toplevel

    key = uuid.uuid4().hex
    retried: list[str] = []

    def retried_once(error: BaseException) -> TerminalError | None:
        if key not in retried and isinstance(error, InvalidUpdateError):
            retried.append(key)
            return None  # the attempt fails, as a crash would, and Restate retries it
        return terminal_of(error)

    monkeypatch.setattr(_graph, "terminal_of", retried_once)
    failed = await chat(harness, "graph_desk", key, f"clash {key}")
    assert failed.status_code == 500 and "invalid state update" in failed.text, failed.text
    assert retried == [key]
    assert (await chat(harness, "graph_desk", key, f"hi {key}")).json() == f"hello {key}"


def test_only_langgraphs_own_errors_end_a_turn():
    """Python's RecursionError, which user code may raise, is retried as any other error."""
    from langgraph.errors import EmptyInputError, GraphRecursionError  # pylint: disable=import-outside-toplevel

    langgraphs: list[Exception] = [GraphRecursionError("limit"), InvalidUpdateError("update"), EmptyInputError("input")]
    assert all(isinstance(terminal_of(error), TerminalError) for error in langgraphs)
    others: list[Exception] = [RecursionError("deep"), ValueError("value"), NotFoundError("missing")]
    assert all(terminal_of(error) is None for error in others)


async def test_a_finished_turn_keeps_the_latest_checkpoint_of_each_namespace(harness: HarnessEnvironment):
    """A run of several steps, then one whose subgraph interrupts twice (a namespace of its own)."""
    key = uuid.uuid4().hex
    assert (await chat(harness, "graph_desk", key, f"crash {key}")).json() == f"done {key}"
    assert (await post(harness, f"graph_desk/{key}/checkpoints")).json() == {"": 1}
    answering = asyncio.ensure_future(chat(harness, "graph_desk", key, "deep"))
    answered: set[str] = set()
    for value in ("A", "B"):
        waits: list[dict[str, Any]] = []
        for _ in range(200):
            waits = [w for w in (await post(harness, f"graph_desk/{key}/pending")).json() if w["id"] not in answered]
            if waits:
                break
            await asyncio.sleep(0.05)
        [wait] = waits
        answered.add(wait["id"])
        await post(harness, f"graph_desk/{key}/signal", json={"id": wait["id"], "value": value})
    assert (await answering).json() == "twice: A B"
    stored = (await post(harness, f"graph_desk/{key}/checkpoints")).json()
    assert len(stored) == 2 and set(stored.values()) == {1}, stored


async def test_a_graph_has_its_own_context_and_the_prebuilt_nodes_still_work(harness: HarnessEnvironment):
    key = uuid.uuid4().hex
    assert (await chat(harness, "graph_context", key, "who")).json() == f"you are {key}"
    answering = asyncio.ensure_future(chat(harness, "graph_context", key, f"refund {key}"))
    [wait] = await until_waiting(harness, key, "graph_context")
    assert wait["name"] == "approval"
    await post(harness, f"graph_context/{key}/signal", json={"name": "approval", "value": "yes"})
    assert (await answering).json() == "approved: yes"


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
    # Any: no overload takes both, as the type checker says.
    make: Any = langgraph.agent
    with pytest.raises(TypeError, match="your own graph is configured .*: pass no system with it"):
        make("both", graph=builder, system="Hi.")
    with pytest.raises(TypeError, match="pass no model, tools with it"):
        make("both", graph=builder, model="openai/gpt-6-sol", tools=[lookup])

    class Plain(TypedDict):
        count: int

    with pytest.raises(TypeError, match="MessagesState"):
        langgraph.agent("plain", graph=StateGraph(Plain))


async def test_only_a_run_stopped_with_no_interrupt_is_at_a_breakpoint():
    """A subgraph's static breakpoint stops a run with no interrupt; an interrupt in a node's second subgraph call
    does not, though LangGraph's subgraph snapshots do not show its state (its namespace is again:<task>|1)."""
    from langgraph.checkpoint.memory import InMemorySaver  # pylint: disable=import-outside-toplevel

    config: Any = {"configurable": {"thread_id": "t"}}
    stopped: dict[str, bool] = {}
    for word in ("pause", "stops", "again"):
        app: Any = builder.compile(checkpointer=InMemorySaver())
        await app.ainvoke({"messages": [HumanMessage(f"{word} x")]}, config, durability="sync")
        snapshot = await app.aget_state(config)
        assert snapshot.next
        stopped[word] = at_breakpoint(snapshot)
    assert stopped == {"pause": True, "stops": True, "again": False}


@pytest.mark.parametrize("breakpoint", [{"interrupt_before": ["tools"]}, {"interrupt_after": ["call_tool"]}])
def test_a_graph_with_static_breakpoints_is_refused(breakpoint: dict[str, Any]):
    """A run stopped at one has nothing to wait for, and would leave its tool calls without results."""
    with pytest.raises(TypeError, match=r"interrupt\(\)"):
        langgraph.agent("stops", graph=builder.compile(**breakpoint))


def test_a_graph_may_still_be_built_after_its_agent(caplog: pytest.LogCaptureFixture):
    """As each run compiles it: checking it neither refuses a graph with nodes still to add, nor marks it compiled."""
    later = StateGraph(Desk)
    later.add_edge(START, "greet")  # to a node not added yet
    langgraph.agent("later", graph=later)
    later.add_node("greet", typing.cast(Any, greet))
    later.add_edge("greet", END)
    assert [r.getMessage() for r in caplog.records if "already been compiled" in r.getMessage()] == []
    langgraph.agent("later_again", graph=later)


def test_context_is_for_a_graph_of_your_own():
    # Any: no overload takes it, as the type checker says.
    make: Any = langgraph.agent
    with pytest.raises(TypeError, match="context is an option of your own graph: pass it with graph="):
        make("plain", context=lambda ctx: Caller(ctx.key))
