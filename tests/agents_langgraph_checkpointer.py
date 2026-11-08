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

import operator
import typing
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Any, TypedDict

import pytest

pytest.importorskip("langgraph")

from langchain_core.runnables import RunnableConfig  # noqa: E402
from langgraph.channels import DeltaChannel  # noqa: E402
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


async def test_prune_keeps_the_latest_checkpoint_and_only_its_values_and_the_thread_goes_on():
    backend = Backend()
    counted: dict[str, Any] = {}

    async def go(ctx: sessions.SessionContext) -> None:
        saver = SqlCheckpointSaver(ctx.db)
        app = steps([]).compile(checkpointer=saver)
        other: RunnableConfig = {"configurable": {"thread_id": "other"}}
        await app.ainvoke({"log": []}, THREAD, durability="sync")
        await app.ainvoke({"log": []}, other, durability="sync")
        await app.ainvoke({"log": []}, THREAD, durability="sync")
        await saver.aprune(["t"])
        for table in ("checkpoints", "channel_values", "writes"):
            row = await ctx.db.get(f"SELECT count(*) AS n FROM restate_langgraph_{table} WHERE thread_id = 't'")
            counted[table] = row["n"] if row else None
        latest = await saver.aget_tuple(THREAD)
        assert latest is not None
        counted["versions"] = len(latest.checkpoint["channel_versions"])
        counted["other"] = len([c async for c in saver.alist(other)])
        counted["log"] = (await app.ainvoke({"log": []}, THREAD, durability="sync"))["log"]

    await attempt(backend, go)
    assert (counted["checkpoints"], counted["writes"]) == (1, 0)
    assert counted["channel_values"] == counted["versions"]
    assert counted["other"] == 5  # another thread keeps its own
    assert sorted(counted["log"]) == sorted(["a", "b", "c", "d"] * 3)


async def test_prune_deletes_threads_and_knows_no_other_strategy():
    backend = Backend()
    seen: dict[str, Any] = {}

    async def go(ctx: sessions.SessionContext) -> None:
        saver = SqlCheckpointSaver(ctx.db)
        await steps([]).compile(checkpointer=saver).ainvoke({"log": []}, THREAD, durability="sync")
        with pytest.raises(ValueError, match="strategy"):
            await saver.aprune(["t"], strategy="keep_some")
        await saver.aprune(["t"], strategy="delete")
        seen["t"] = await saver.aget_tuple(THREAD)

    await attempt(backend, go)
    assert seen["t"] is None


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


async def checkpoints(ctx: sessions.SessionContext) -> dict[str, int]:
    """How many checkpoints each namespace has."""
    rows = await ctx.db.values(
        "SELECT checkpoint_ns, count(*) FROM restate_langgraph_checkpoints GROUP BY checkpoint_ns"
    )
    return {str(ns): int(typing.cast(int, n)) for ns, n in rows}


@pytest.mark.parametrize("durability", ["sync", "async", "exit"])
async def test_a_subgraph_paused_and_pruned_resumes_whole_and_each_namespace_keeps_its_latest(durability: Any):
    backend = Backend()
    ran: list[str] = []
    seen: dict[str, Any] = {}

    async def go(ctx: sessions.SessionContext) -> None:
        saver = SqlCheckpointSaver(ctx.db)
        app = nested(ran, ask=True, before=3).compile(checkpointer=saver)
        await app.ainvoke({"log": [], "topic": "T"}, THREAD, durability=durability)
        await saver.aprune(["t"])
        seen["paused"] = await checkpoints(ctx)
        seen["log"] = (await app.ainvoke(Command(resume="yes"), THREAD, durability=durability))["log"]
        await saver.aprune(["t"])
        seen["done"] = await checkpoints(ctx)

    await attempt(backend, go)
    assert sorted(seen["paused"].values()) == [1, 1]  # the graph's, and the paused subgraph run's
    # The subgraph resumed from its checkpoint, whole: p, before the interrupt, ran once, and the topic (which only
    # its first checkpoint wrote) is there.
    assert ("x:yes:T" in seen["log"], ran) == (True, ["p"])
    assert set(seen["done"].values()) == {1}


async def test_a_run_paused_below_a_subgraph_with_its_own_history_resumes_after_a_prune():
    """Such a subgraph resumes its paused step after the graph above it moved on (here, by update_state)."""
    backend = Backend()
    ran: list[str] = []
    seen: dict[str, Any] = {}

    async def go(ctx: sessions.SessionContext) -> None:
        saver = SqlCheckpointSaver(ctx.db)
        app = nested(ran, ask=True, before=1, own_history=True).compile(checkpointer=saver)
        await app.ainvoke({"log": [], "topic": "T"}, THREAD, durability="sync")
        await app.aupdate_state(THREAD, {"log": ["updated"]}, as_node="s0")
        await app.ainvoke(None, THREAD, durability="sync")
        await saver.aprune(["t"])
        seen["log"] = (await app.ainvoke(Command(resume="yes"), THREAD, durability="sync"))["log"]

    await attempt(backend, go)
    assert ("x:yes:T" in seen["log"], ran) == (True, ["p"])


def twice(_: State) -> dict[str, list[str]]:
    return {"log": [f"{interrupt('one')},{interrupt('two')}"]}


async def test_langgraph_gives_each_interrupt_of_a_run_an_id_of_its_own():
    """What the waits of ``interrupt()`` count on (``_graph._resumed``): a wait per interrupt id.

    A node that interrupts again, after it was resumed, raises an interrupt
    of an id of its own: LangGraph makes it of the namespace of the task
    that raised it and the number of resume values that task has. That
    holds in a subgraph that a node calls after interrupting itself: the
    node lists the subgraph's interrupt again, under the raiser's id.
    """
    inner = StateGraph(State)
    inner.add_node("twice", typing.cast(Any, twice))
    inner.add_edge(START, "twice")
    inner.add_edge("twice", END)
    asking: Any = inner.compile()

    async def approve(_: State) -> dict[str, list[str]]:
        approved = interrupt("approve?")
        return {"log": [f"{approved}:{(await asking.ainvoke({'log': []}))['log'][-1]}"]}

    outer = StateGraph(State)
    outer.add_node("approve", typing.cast(Any, approve))
    outer.add_edge(START, "approve")
    outer.add_edge("approve", END)
    backend = Backend()
    seen: list[tuple[str, Any]] = []
    final: dict[str, Any] = {}

    async def go(ctx: sessions.SessionContext) -> None:
        saver = SqlCheckpointSaver(ctx.db)
        app: Any = outer.compile(checkpointer=saver)
        await app.ainvoke({"log": []}, THREAD, durability="sync")
        for value in ("yes", "1", "2"):
            snapshot = await app.aget_state(THREAD)
            [pending] = {found.id: found for task in snapshot.tasks for found in task.interrupts}.values()
            seen.append((pending.id, pending.value))
            await app.ainvoke(Command(resume={pending.id: value}), THREAD, durability="sync")
        final["log"] = (await app.aget_state(THREAD)).values["log"]

    await attempt(backend, go)
    assert [value for _, value in seen] == ["approve?", "one", "two"]
    assert len({interrupt_id for interrupt_id, _ in seen}) == 3
    assert final["log"] == ["yes:1,2"]


def appended(log: list[str] | None, writes: Sequence[list[str]]) -> list[str]:
    """A ``DeltaChannel``'s reducer: the writes, appended in order."""
    return [*(log or []), *(entry for write in writes for entry in write)]


@pytest.mark.parametrize("every", [2, 100])
async def test_a_prune_keeps_what_a_delta_channel_is_rebuilt_from(every: int):
    """A ``DeltaChannel`` (LangGraph's beta) is rebuilt from its latest snapshot and the writes after it.

    Each run writes the log three times: with a snapshot every second write,
    the latest checkpoint needs at most its parent; with one every hundredth,
    every checkpoint since the first.
    """

    class Delta(TypedDict):
        log: Annotated[list[str], DeltaChannel(appended, snapshot_frequency=every)]

    graph = StateGraph(Delta)
    graph.add_node("a", typing.cast(Any, lambda _: {"log": ["a"]}))
    graph.add_node("b", typing.cast(Any, lambda _: {"log": ["b"]}))
    graph.add_edge(START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)
    backend = Backend()
    seen: list[tuple[list[str], int]] = []

    async def go(ctx: sessions.SessionContext) -> None:
        saver = SqlCheckpointSaver(ctx.db)
        app: Any = graph.compile(checkpointer=saver)
        for n in range(3):
            await app.ainvoke({"log": [str(n)]}, THREAD, durability="sync")
            await saver.aprune(["t"])
            [(stored,)] = await ctx.db.values("SELECT count(*) FROM restate_langgraph_checkpoints")
            seen.append(((await app.aget_state(THREAD)).values["log"], int(typing.cast(int, stored))))

    await attempt(backend, go)
    runs = [[str(n), "a", "b"] for n in range(3)]
    assert [log for log, _ in seen] == [runs[0], runs[0] + runs[1], runs[0] + runs[1] + runs[2]]
    kept = [stored for _, stored in seen]
    assert max(kept) <= 2 if every == 2 else kept == [4, 8, 12]


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
