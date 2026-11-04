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
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, TypedDict

import pytest

pytest.importorskip("langgraph")

from langchain_core.runnables import RunnableConfig  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402

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
    # a ran once; b's write, stored while c failed, is not made again; c runs again.
    assert ran == ["a", "b", "c", "c", "d"] or ran == ["a", "c", "b", "c", "d"]
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
