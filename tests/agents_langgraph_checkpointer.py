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
"""restate.agents.langgraph's checkpointer: LangGraph's checkpoints in a session object's database."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import asyncio
import contextlib
import operator
import typing
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any, TypedDict

import pytest

pytest.importorskip("langgraph")

from langchain_core.runnables import RunnableConfig  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402

from restate import sessions  # noqa: E402
from restate.agents.langgraph import SqlCheckpointSaver  # noqa: E402
from sessions_database import Backend  # noqa: E402  # pylint: disable=import-error
from sessions_object import Host, run  # noqa: E402  # pylint: disable=import-error


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]


class State(TypedDict):
    log: Annotated[list[str], operator.add]


# Each test's work, run by the handler `work` with the attempt's context.
Work = Callable[[sessions.SessionContext], Awaitable[None]]
job: list[Work] = []

graphs = sessions.SessionObject("graphs")


@graphs.handler
async def work(ctx: sessions.SessionContext) -> None:
    await job[-1](ctx)


async def attempt(backend: Backend, work: Work) -> None:
    """An attempt of ``work`` on ``backend``'s key."""
    job.append(work)
    await run(graphs, "work", Host(backend, service="graphs"))


THREAD: RunnableConfig = {"configurable": {"thread_id": "t"}}


def steps(ran: list[str], fail: set[str] | None = None) -> Any:
    """a, then b and c at once, then d: each records that it ran; those in ``fail`` fail once."""
    failing = set(fail or ())

    def node(name: str) -> Callable[[State], dict[str, list[str]]]:
        def step(_: State) -> dict[str, list[str]]:
            ran.append(name)
            if name in failing:
                failing.discard(name)
                raise RuntimeError(f"{name} crashed")
            return {"log": [name]}

        return step

    graph = StateGraph(State)
    for name in "abcd":
        graph.add_node(name, typing.cast(Any, node(name)))
    graph.add_edge(START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("a", "c")
    graph.add_edge(["b", "c"], "d")
    graph.add_edge("d", END)
    return graph


async def test_a_retried_attempt_resumes_from_the_last_stored_step_and_its_tasks_writes():
    backend = Backend()
    ran: list[str] = []
    graph = steps(ran, fail={"c"})
    results: list[Any] = []

    async def go(ctx: sessions.SessionContext) -> None:
        app = graph.compile(checkpointer=SqlCheckpointSaver(ctx.db))
        state = await app.aget_state(THREAD)
        results.append(await app.ainvoke(None if state.next else {"log": []}, THREAD, durability="sync"))

    with pytest.raises(RuntimeError, match="c crashed"):
        await attempt(backend, go)
    await attempt(backend, go)
    # a ran once; b ran once (its write, stored if it finished before c failed, is not made again; if c failed
    # before b started, b runs in the retry); c runs again; d runs last.
    assert (ran[0], ran[-1]) == ("a", "d")
    assert {step: ran.count(step) for step in "abcd"} == {"a": 1, "b": 1, "c": 2, "d": 1}
    assert sorted(results[-1]["log"]) == ["a", "b", "c", "d"]


async def test_history_lists_the_checkpoints_newest_first_by_limit_filter_and_before():
    backend = Backend()
    listed: dict[str, list[Any]] = {}

    async def go(ctx: sessions.SessionContext) -> None:
        saver = SqlCheckpointSaver(ctx.db)
        app = steps([]).compile(checkpointer=saver)
        await app.ainvoke({"log": []}, THREAD, durability="sync")
        listed["all"] = [c async for c in saver.alist(THREAD)]
        listed["two"] = [c async for c in saver.alist(THREAD, limit=2)]
        listed["step 1"] = [c async for c in saver.alist(THREAD, filter={"step": 1})]
        listed["before"] = [c async for c in saver.alist(THREAD, before=listed["all"][1].config)]
        listed["other"] = [c async for c in saver.alist({"configurable": {"thread_id": "other"}})]

    await attempt(backend, go)
    stepped = [c.metadata.get("step") for c in listed["all"]]
    assert stepped == sorted(stepped, reverse=True) and len(stepped) == 5
    assert [c.config for c in listed["two"]] == [c.config for c in listed["all"][:2]]
    assert [c.metadata.get("step") for c in listed["step 1"]] == [1]
    assert [c.config for c in listed["before"]] == [c.config for c in listed["all"][2:]]
    assert listed["other"] == []
    # Each checkpoint names its parent.
    assert listed["all"][0].parent_config == listed["all"][1].config


async def test_latest_history_keeps_one_checkpoint_and_only_its_values():
    backend = Backend()
    counted: dict[str, Any] = {}

    async def go(ctx: sessions.SessionContext) -> None:
        saver = SqlCheckpointSaver(ctx.db, history="latest")
        app = steps([]).compile(checkpointer=saver)
        await app.ainvoke({"log": []}, THREAD, durability="sync")
        await app.ainvoke({"log": []}, THREAD, durability="sync")
        for table in ("checkpoints", "channel_values", "writes"):
            row = await ctx.db.get(f"SELECT count(*) AS n FROM restate_langgraph_{table}")
            counted[table] = row["n"] if row else None
        latest = await saver.aget_tuple(THREAD)
        assert latest is not None
        counted["versions"] = len(latest.checkpoint["channel_versions"])
        counted["log"] = latest.checkpoint["channel_values"]["log"]

    await attempt(backend, go)
    assert counted["checkpoints"] == 1
    assert counted["channel_values"] == counted["versions"]
    assert sorted(counted["log"]) == sorted(["a", "b", "c", "d"] * 2)


class Topic(State):
    topic: str
    """Written once, by the input: a subgraph's run stores it in its first checkpoint only."""


def nested(ran: list[str], *, ask: bool = False, before: int = 0, own_history: bool = False) -> Any:
    """A graph whose last node is a subgraph: ``p``, which records that it ran, then ``x``, which logs the topic.

    ``x`` asks with ``interrupt()`` if ``ask``. ``before`` steps of the graph come first. With ``own_history``, the
    subgraph runs inside one that keeps its own history (``checkpointer=True``), the node ``mid``.
    """

    def p(_: Topic) -> dict[str, list[str]]:
        ran.append("p")
        return {"log": ["p"]}

    def x(state: Topic) -> dict[str, list[str]]:
        return {"log": [f"x:{interrupt('go?') if ask else 'ran'}:{state['topic']}"]}

    inner = StateGraph(Topic)
    inner.add_node("p", typing.cast(Any, p))
    inner.add_node("x", typing.cast(Any, x))
    inner.add_edge(START, "p")
    inner.add_edge("p", "x")
    inner.add_edge("x", END)
    last, node = "inner", typing.cast(Any, inner.compile())
    if own_history:
        mid = StateGraph(Topic)
        mid.add_node("inner", node)
        mid.add_edge(START, "inner")
        mid.add_edge("inner", END)
        last, node = "mid", mid.compile(checkpointer=True)
    outer = StateGraph(Topic)
    previous = START
    for n in range(before):
        outer.add_node(f"s{n}", typing.cast(Any, lambda _: {"log": []}))
        outer.add_edge(previous, f"s{n}")
        previous = f"s{n}"
    outer.add_node(last, node)
    outer.add_edge(previous, last)
    outer.add_edge(last, END)
    return outer


class Slow:
    """A database each of whose reads and transactions waits a little: LangGraph's async puts lag behind its steps."""

    def __init__(self, db: sessions.Database) -> None:
        self._db = db

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

    async def values(self, *args: Any) -> Any:
        await asyncio.sleep(0.002)
        return await self._db.values(*args)

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        await asyncio.sleep(0.002)
        async with self._db.transaction() as tx:
            yield tx


def saver(ctx: sessions.SessionContext) -> SqlCheckpointSaver:
    return SqlCheckpointSaver(typing.cast(Any, Slow(ctx.db)), history="latest")


async def namespaces(ctx: sessions.SessionContext) -> set[str]:
    """The checkpoint namespaces that any of the saver's tables has rows of."""
    found: set[str] = set()
    for table in ("checkpoints", "channel_values", "writes"):
        found |= {str(row[0]) for row in await ctx.db.values(f"SELECT checkpoint_ns FROM restate_langgraph_{table}")}
    return found


DURABILITIES = ["sync", "async", "exit"]


@pytest.mark.parametrize("durability", DURABILITIES)
async def test_latest_history_keeps_no_subgraph_runs_of_steps_that_are_done(durability: Any):
    backend = Backend()
    seen: dict[str, Any] = {}

    async def go(ctx: sessions.SessionContext) -> None:
        app = nested([], before=3).compile(checkpointer=saver(ctx))
        for _ in range(3):
            seen["log"] = (await app.ainvoke({"log": [], "topic": "T"}, THREAD, durability=durability))["log"]
        seen["namespaces"] = await namespaces(ctx)

    await attempt(backend, go)
    assert "x:ran:T" in seen["log"]
    # Each subgraph run is a namespace of its own (node:task_id): none is left once its step is done.
    assert seen["namespaces"] == {""}


@pytest.mark.parametrize("durability", DURABILITIES)
async def test_latest_history_keeps_a_paused_subgraph_until_its_step_is_done(durability: Any):
    """With "async", the graph's checkpoints are stored after its later steps start; with "exit", once, as it pauses."""
    backend = Backend()
    ran: list[str] = []
    seen: dict[str, Any] = {}

    async def go(ctx: sessions.SessionContext) -> None:
        app = nested(ran, ask=True, before=6).compile(checkpointer=saver(ctx))
        await app.ainvoke({"log": [], "topic": "T"}, THREAD, durability=durability)
        seen["paused"] = await namespaces(ctx)
        seen["log"] = (await app.ainvoke(Command(resume="yes"), THREAD, durability=durability))["log"]
        seen["done"] = await namespaces(ctx)

    await attempt(backend, go)
    assert len(seen["paused"]) == 2  # the graph's, and the paused subgraph's
    # The subgraph resumed from its checkpoints, whole: p, before the interrupt, ran once, and the topic is there.
    assert ("x:yes:T" in seen["log"], ran) == (True, ["p"])
    assert seen["done"] == {""}


async def test_latest_history_keeps_a_paused_run_below_a_subgraph_with_its_own_history():
    """Such a subgraph resumes its paused step after the graph above it moved on (here, by update_state)."""
    backend = Backend()
    ran: list[str] = []
    seen: dict[str, Any] = {}

    async def go(ctx: sessions.SessionContext) -> None:
        app = nested(ran, ask=True, before=1, own_history=True).compile(checkpointer=saver(ctx))
        await app.ainvoke({"log": [], "topic": "T"}, THREAD, durability="sync")
        await app.aupdate_state(THREAD, {"log": ["updated"]}, as_node="s0")
        await app.ainvoke(None, THREAD, durability="sync")
        seen["log"] = (await app.ainvoke(Command(resume="yes"), THREAD, durability="sync"))["log"]
        seen["done"] = await namespaces(ctx)

    await attempt(backend, go)
    assert ("x:yes:T" in seen["log"], ran) == (True, ["p"])
    # The run below it is deleted once that subgraph's step is done; the subgraph keeps its own history.
    assert seen["done"] == {"", "mid"}


async def test_a_fork_from_an_earlier_checkpoint_leaves_the_branch_it_forked_from():
    backend = Backend()
    seen: dict[str, Any] = {}

    async def go(ctx: sessions.SessionContext) -> None:
        saver = SqlCheckpointSaver(ctx.db)
        app = steps([]).compile(checkpointer=saver)
        await app.ainvoke({"log": []}, THREAD, durability="sync")
        history = [c async for c in saver.alist(THREAD)]
        after_a = next(c for c in history if c.metadata.get("step") == 1)
        forked = await app.aupdate_state(after_a.config, {"log": ["fork"]})
        seen["fork"] = (await app.aget_state(forked)).values["log"]
        seen["main"] = (await app.aget_state(history[0].config)).values["log"]

    await attempt(backend, go)
    assert seen["fork"] == ["a", "fork"]
    assert sorted(seen["main"]) == ["a", "b", "c", "d"]


async def test_delete_thread_forgets_it_and_no_other():
    backend = Backend()
    seen: dict[str, Any] = {}

    async def go(ctx: sessions.SessionContext) -> None:
        saver = SqlCheckpointSaver(ctx.db)
        app = steps([]).compile(checkpointer=saver)
        other: RunnableConfig = {"configurable": {"thread_id": "other"}}
        await app.ainvoke({"log": []}, THREAD, durability="sync")
        await app.ainvoke({"log": []}, other, durability="sync")
        await saver.adelete_thread("t")
        seen["t"] = await saver.aget_tuple(THREAD)
        seen["other"] = await saver.aget_tuple(other)

    await attempt(backend, go)
    assert seen["t"] is None
    assert typing.cast(Any, seen["other"]).checkpoint["channel_values"]["log"]
