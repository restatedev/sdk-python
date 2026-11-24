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
"""The checkpointer stores a task's pending writes in the transaction of the checkpoint that follows them, when it
follows at once, and on their own after a few event-loop iterations otherwise; an ``aput_writes`` returns once its rows
are stored.

Every test runs twice: with LangGraph starting its tasks at once (Python 3.12 and later) and on the next iteration (what
3.11 does), which the second run emulates.
"""

# pylint: disable=missing-function-docstring,missing-class-docstring,protected-access

import asyncio
import gc
import operator
import typing
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Annotated, Any, TypedDict

import pytest

pytest.importorskip("langgraph")

from langchain_core.runnables import RunnableConfig  # noqa: E402
from langgraph._internal import _future  # noqa: E402
from langgraph.checkpoint.base import Checkpoint, empty_checkpoint  # noqa: E402
from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import Command, interrupt  # noqa: E402

from restate import sessions  # noqa: E402
from restate.agents.langgraph import SqlCheckpointSaver  # noqa: E402
from agents_langgraph_checkpointer import THREAD, attempt  # noqa: E402  # pylint: disable=import-error
from sessions_database import Backend  # noqa: E402  # pylint: disable=import-error
from transactions_spy import Spy  # noqa: E402  # pylint: disable=import-error

WRITES = "restate_langgraph_writes"
CHECKPOINTS = "restate_langgraph_checkpoints"


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True, params=["tasks at once", "tasks on the next iteration"])
def lazy_tasks(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> bool:
    """LangGraph starts the coroutines it submits at once on Python 3.12 and later, and on the next iteration before."""
    lazy = request.param != "tasks at once"
    if lazy:
        monkeypatch.setattr(_future, "EAGER_NOT_SUPPORTED", True)
    return lazy


pytestmark = [pytest.mark.anyio]


@pytest.fixture
async def unhandled() -> AsyncIterator[list[dict[str, Any]]]:
    """What the event loop's exception handler was given while the test ran: a task or a future whose exception
    nobody retrieved, say. A test ends by asserting that it is empty."""
    loop = asyncio.get_running_loop()
    found: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: found.append(context))
    yield found
    for _ in range(10):
        await asyncio.sleep(0)
    gc.collect()
    loop.set_exception_handler(None)
    assert not found


async def iterations(count: int) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


# --- The acceptance test: a parallel step cut while one of its nodes runs -----------------------------------------


class State(TypedDict):
    par: Annotated[list[str], operator.add]


class HoldsWrites(SqlCheckpointSaver):
    """The control: holds every write until the next ``aput``, and returns at once."""

    def __init__(self, db: Any) -> None:
        super().__init__(db)
        self.held: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def aput_writes(self, *args: Any, **kwargs: Any) -> None:
        self.held.append((args, kwargs))

    async def aput(self, *args: Any, **kwargs: Any) -> RunnableConfig:
        held, self.held = self.held, []
        for held_args, held_kwargs in held:
            await super().aput_writes(*held_args, **held_kwargs)
        return await super().aput(*args, **kwargs)


def parallel(ran: dict[str, int], gate: asyncio.Event | None = None, fail: bool = False) -> Any:
    """START, then x and y at once, then join: y waits for ``gate``, and fails if it is told to."""

    async def x(_: State) -> dict[str, list[str]]:
        ran["x"] += 1
        return {"par": ["x"]}

    async def y(_: State) -> dict[str, list[str]]:
        ran["y"] += 1
        if gate is not None:
            await gate.wait()
        if fail:
            raise RuntimeError("y failed")
        return {"par": ["y"]}

    async def join(_: State) -> dict[str, list[str]]:
        ran["join"] += 1
        return {}

    graph = StateGraph(State)
    graph.add_node("x", typing.cast(Any, x))
    graph.add_node("y", typing.cast(Any, y))
    graph.add_node("join", typing.cast(Any, join))
    graph.add_edge(START, "x")
    graph.add_edge(START, "y")
    graph.add_edge("x", "join")
    graph.add_edge("y", "join")
    graph.add_edge("join", END)
    return graph


async def crash_while_y_runs(saver: Callable[[Any], SqlCheckpointSaver]) -> tuple[dict[str, int], list[str]]:
    """Cut the run while y runs, with x finished, and resume from a copy of what was stored then."""
    backend = Backend()
    ran = {"x": 0, "y": 0, "join": 0}
    gate = asyncio.Event()
    image: dict[str, bytes] = {}

    async def first(ctx: sessions.SessionContext) -> None:
        app = parallel(ran, gate, fail=True).compile(checkpointer=saver(ctx.db))
        run = asyncio.create_task(app.ainvoke({"par": []}, THREAD, durability="sync"))
        while ran["x"] == 0 or ran["y"] == 0:
            await asyncio.sleep(0)
        await iterations(300)  # x's commit, and the writes' wait, are far inside this many iterations
        image.update(backend.stored)  # what is durable now: the crash
        gate.set()
        with pytest.raises(RuntimeError, match="y failed"):
            await run

    await attempt(backend, first)
    resumed = Backend()
    resumed.stored = dict(image)  # a new process: the copy only
    result: dict[str, Any] = {}

    async def second(ctx: sessions.SessionContext) -> None:
        app = parallel(ran).compile(checkpointer=saver(ctx.db))
        result.update(await app.ainvoke(None, THREAD, durability="sync"))

    await attempt(resumed, second)
    return ran, sorted(result["par"])


async def test_a_node_that_finished_does_not_run_again_after_a_crash_while_its_sibling_runs():
    ran, par = await crash_while_y_runs(SqlCheckpointSaver)
    assert ran == {"x": 1, "y": 2, "join": 1}
    assert par == ["x", "y"]


async def test_a_saver_that_holds_the_writes_until_the_next_put_runs_it_again_so_the_test_discriminates():
    ran, _ = await crash_while_y_runs(HoldsWrites)
    assert ran["x"] == 2


async def test_a_node_that_finished_does_not_run_again_after_the_attempt_fails_on_the_same_database():
    backend = Backend()
    ran = {"x": 0, "y": 0, "join": 0}

    async def first(ctx: sessions.SessionContext) -> None:
        app = parallel(ran, fail=True).compile(checkpointer=SqlCheckpointSaver(ctx.db))
        with pytest.raises(RuntimeError, match="y failed"):
            await app.ainvoke({"par": []}, THREAD, durability="sync")

    async def second(ctx: sessions.SessionContext) -> None:
        app = parallel(ran).compile(checkpointer=SqlCheckpointSaver(ctx.db))
        assert sorted((await app.ainvoke(None, THREAD, durability="sync"))["par"]) == ["x", "y"]

    await attempt(backend, first)
    await attempt(backend, second)
    assert ran == {"x": 1, "y": 2, "join": 1}


# --- A handler that returns right after the run: what the run leaves stored ----------------------------------------


class Log(TypedDict):
    log: Annotated[list[str], operator.add]


def interrupting(kind: str) -> Any:
    """a, then b, which interrupts or fails."""

    async def a(_: Log) -> dict[str, list[str]]:
        return {"log": ["a"]}

    async def b(_: Log) -> dict[str, list[str]]:
        if kind == "error":
            raise RuntimeError("b failed")
        return {"log": [str(interrupt("question?"))]}

    graph = StateGraph(Log)
    graph.add_node("a", typing.cast(Any, a))
    graph.add_node("b", typing.cast(Any, b))
    graph.add_edge(START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)
    return graph


async def stored_writes(image: dict[str, bytes]) -> list[tuple[str, str]]:
    """The writes in a copy of the store, as a new process would read them: (task, channel) of each."""
    copy = Backend()
    copy.stored = dict(image)
    found: list[tuple[str, str]] = []

    async def read(ctx: sessions.SessionContext) -> None:
        found.extend(
            (str(task), str(channel))
            for task, channel in await ctx.db.values("SELECT task_id, channel FROM restate_langgraph_writes")
        )

    await attempt(copy, read)
    return found


@pytest.mark.parametrize("durability", ["sync", "async", "exit"])
@pytest.mark.parametrize("kind", ["interrupt", "error"])
async def test_a_handler_that_returns_right_after_the_run_has_its_interrupt_or_error_stored(
    kind: str, durability: typing.Literal["sync", "async", "exit"], unhandled: list[dict[str, Any]]
):
    backend = Backend()
    image: dict[str, bytes] = {}

    async def handler(ctx: sessions.SessionContext) -> None:
        app = interrupting(kind).compile(checkpointer=SqlCheckpointSaver(ctx.db))
        try:
            await app.ainvoke({"log": []}, THREAD, durability=durability)
        except RuntimeError:
            assert kind == "error"
        image.update(backend.stored)  # the handler returns at once: this is what it leaves

    await attempt(backend, handler)
    channels = {channel for _, channel in await stored_writes(image)}
    assert ("__interrupt__" if kind == "interrupt" else "__error__") in channels
    assert not unhandled

    if kind == "interrupt":  # a new saver over the same database shows the interrupt, and the run resumes from it
        copy = Backend()
        copy.stored = dict(image)
        shown: list[Any] = []

        async def resume(ctx: sessions.SessionContext) -> None:
            app = interrupting(kind).compile(checkpointer=SqlCheckpointSaver(ctx.db))
            state = await app.aget_state(THREAD)
            shown.extend(i.value for task in state.tasks for i in task.interrupts)
            result = await app.ainvoke(Command(resume="yes"), THREAD, durability=durability)
            assert result["log"] == ["a", "yes"]

        await attempt(copy, resume)
        assert shown == ["question?"]


# --- The saver's own operations ------------------------------------------------------------------------------------


def checkpoint_config(checkpoint: Checkpoint) -> RunnableConfig:
    return {"configurable": {"thread_id": "t", "checkpoint_ns": "", "checkpoint_id": checkpoint["id"]}}


class Pair:
    """A saver over a recording database, with a first checkpoint stored, whose writes the test makes."""

    def __init__(self, ctx: sessions.SessionContext, backend: Backend) -> None:
        self.spy = Spy(ctx.db)
        self.saver = SqlCheckpointSaver(self.spy.database)
        self.backend = backend
        self.first = empty_checkpoint()
        self.at = checkpoint_config(self.first)

    async def start(self) -> None:
        """Store the first checkpoint, whose writes the test makes next."""
        await self.saver.aput({"configurable": {"thread_id": "t", "checkpoint_ns": ""}}, self.first, {}, {})
        self.spy.commits.clear()

    def write(self, task: str, *channels: str) -> asyncio.Task[None]:
        """Call ``aput_writes`` for ``task`` as a task of its own: it returns once its rows are stored."""
        values = [(channel, f"{task}:{channel}") for channel in channels]
        return asyncio.ensure_future(self.saver.aput_writes(self.at, values, task, f"~{task}"))

    def put(self) -> asyncio.Task[RunnableConfig]:
        """Call ``aput`` of the checkpoint that follows the first, as a task of its own."""
        following = empty_checkpoint()
        return asyncio.ensure_future(self.saver.aput(self.at, following, {"step": 1}, {}))

    def rows(self) -> list[Any]:
        """The writes' rows of the commits, in order: a list of (task, channel)."""
        return [
            (row[3], row[6])
            for commit in self.spy.commits
            for table, row in commit
            if table == WRITES and len(row) == 9
        ]

    def image(self) -> dict[str, bytes]:
        return dict(self.backend.stored)


async def with_pair(body: Callable[[Pair], Coroutine[Any, Any, None]]) -> None:
    backend = Backend()

    async def handler(ctx: sessions.SessionContext) -> None:
        pair = Pair(ctx, backend)
        await pair.start()
        await body(pair)

    await attempt(backend, handler)


async def test_writes_and_the_put_that_follows_at_once_are_one_commit_with_the_writes_first_in_call_order(
    unhandled: list[dict[str, Any]],
):
    async def body(pair: Pair) -> None:
        first, second = pair.write("a", "x", "y"), pair.write("b", "z")
        put = pair.put()
        await asyncio.gather(first, second, put)
        assert pair.spy.tables == [[WRITES, CHECKPOINTS]]
        assert pair.rows() == [("a", "x"), ("a", "y"), ("b", "z")]
        assert [table for table, _ in pair.spy.commits[0]] == [WRITES, WRITES, WRITES, CHECKPOINTS]
        # The first checkpoint shows them as LangGraph expects: (task, channel, value) in its order.
        found = await pair.saver.aget_tuple(pair.at)
        assert found is not None
        assert found.pending_writes == [("a", "x", "a:x"), ("a", "y", "a:y"), ("b", "z", "b:z")]

    await with_pair(body)
    assert not unhandled


async def test_a_write_that_no_put_follows_is_stored_alone_after_a_few_iterations_and_returns_then(
    unhandled: list[dict[str, Any]],
):
    async def body(pair: Pair) -> None:
        write = pair.write("a", "x")
        await asyncio.sleep(0)
        assert not write.done() and pair.spy.commits == []  # it waits: a put may follow
        await write
        assert pair.spy.tables == [[WRITES]]
        # A second connection over the same store has the rows when the write returned.
        assert await stored_writes(pair.image()) == [("a", "x")]

    await with_pair(body)
    assert not unhandled


async def test_a_write_awaited_before_the_put_is_called_does_not_wait_for_it_and_is_a_commit_of_its_own(
    unhandled: list[dict[str, Any]],
):
    async def body(pair: Pair) -> None:  # the order of a delta channel's write, an error handler's, update_state's
        await pair.write("a", "x")
        await pair.put()
        assert pair.spy.tables == [[WRITES], [CHECKPOINTS]]

    await with_pair(body)
    assert not unhandled


async def test_writes_of_one_task_called_twice_in_a_commit_keep_their_verbs():
    async def body(pair: Pair) -> None:
        # A special write replaces the one before; the others keep the first.
        first = asyncio.ensure_future(pair.saver.aput_writes(pair.at, [("__error__", "one")], "a", ""))
        second = asyncio.ensure_future(pair.saver.aput_writes(pair.at, [("__error__", "two")], "a", ""))
        third = asyncio.ensure_future(pair.saver.aput_writes(pair.at, [("c", "one")], "b", ""))
        fourth = asyncio.ensure_future(pair.saver.aput_writes(pair.at, [("c", "two")], "b", ""))
        await asyncio.gather(first, second, third, fourth, pair.put())
        found = await pair.saver.aget_tuple(pair.at)
        assert found is not None
        assert found.pending_writes == [("a", "__error__", "two"), ("b", "c", "one")]

    await with_pair(body)


async def test_reads_prune_and_delete_store_what_is_pending_first(unhandled: list[dict[str, Any]]):
    async def body(pair: Pair) -> None:
        write = pair.write("a", "x")
        await asyncio.sleep(0)  # the write has been called
        found = await pair.saver.aget_tuple(pair.at)  # in the same iteration as the write
        assert found is not None and found.pending_writes == [("a", "x", "a:x")]
        assert write.done() or pair.spy.tables == [[WRITES]]

        other = pair.write("b", "y")
        await asyncio.sleep(0)
        listed = [c async for c in pair.saver.alist(pair.at)]
        assert listed[0].pending_writes == [("a", "x", "a:x"), ("b", "y", "b:y")]
        assert pair.spy.tables == [[WRITES], [WRITES]]

        third = pair.write("c", "z")
        await asyncio.sleep(0)
        await pair.saver.aprune(["t"])  # the first checkpoint is the latest: it keeps its writes
        assert pair.spy.tables == [
            [WRITES],
            [WRITES],
            [WRITES],
            [CHECKPOINTS, WRITES, "restate_langgraph_channel_values"],
        ]
        assert len(await stored_writes(pair.image())) == 3

        last = pair.write("d", "w")
        await asyncio.sleep(0)
        await pair.saver.adelete_thread("t")
        await asyncio.gather(write, other, third, last)
        assert await stored_writes(pair.image()) == []  # the last write was stored before the delete, not after
        assert pair.rows() == [("a", "x"), ("b", "y"), ("c", "z"), ("d", "w")]

    await with_pair(body)
    assert not unhandled


async def test_a_lone_store_that_fails_fails_the_writes_it_carried_and_the_next_call_goes_ahead(
    unhandled: list[dict[str, Any]],
):
    async def body(pair: Pair) -> None:
        pair.spy.fail = lambda tables: tables == [WRITES]
        first, second = pair.write("a", "x"), pair.write("b", "y")
        for write in (first, second):
            with pytest.raises(RuntimeError, match="the commit failed"):
                await write
        pair.spy.fail = None
        assert pair.spy.commits == []  # nothing was stored, and it is not tried again
        await pair.write("c", "z")
        assert await stored_writes(pair.image()) == [("c", "z")]
        await iterations(20)
        assert pair.rows() == [("c", "z")]

    await with_pair(body)
    assert not unhandled


async def test_a_put_that_fails_fails_itself_and_the_writes_it_carried_and_the_next_calls_go_ahead(
    unhandled: list[dict[str, Any]],
):
    async def body(pair: Pair) -> None:
        pair.spy.fail = lambda tables: CHECKPOINTS in tables
        write = pair.write("a", "x")
        put = pair.put()
        for call in (write, put):
            with pytest.raises(RuntimeError, match="the commit failed"):
                await call
        pair.spy.fail = None
        await iterations(20)
        assert pair.spy.commits == []
        await pair.write("b", "y")
        await pair.put()
        assert pair.rows() == [("b", "y")]
        assert await stored_writes(pair.image()) == [("b", "y")]

    await with_pair(body)
    assert not unhandled


async def test_a_flush_that_has_begun_is_stored_before_a_put_called_in_the_same_iteration(
    lazy_tasks: bool, unhandled: list[dict[str, Any]]
):
    """The write's rows wait, and are stored on their own just as a put is called, in the iteration where the wait
    ended; the put is started at once (as LangGraph's are, on Python 3.12 and later), so it could take the database
    first. The rows of the writes called before the put come first."""
    if not lazy_tasks and not hasattr(asyncio, "eager_task_factory"):
        pytest.skip("Python 3.11 has no eager tasks: LangGraph never starts a task at once there")

    async def body(pair: Pair) -> None:
        loop = asyncio.get_running_loop()
        first = pair.write("a", "x")
        await asyncio.sleep(0)  # the write has been called
        results: list[asyncio.Task[Any]] = []

        def start(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
            if lazy_tasks:
                return loop.create_task(coro)
            return asyncio.eager_task_factory(loop, coro)  # type: ignore[attr-defined,no-any-return]

        def late(count: int) -> None:
            if count:  # our own chain of iterations, one handle at a time, behind the write's
                loop.call_soon(late, count - 1)
                return
            results.append(start(pair.saver.aput_writes(pair.at, [("y", "late")], "b", "~b")))
            results.append(start(pair.saver.aput(pair.at, empty_checkpoint(), {"step": 1}, {})))

        loop.call_soon(late, 2)
        await asyncio.gather(first, *results[:0])
        for _ in range(100):  # bounded: a callback that failed must fail the test, not hang it
            if len(results) == 2:
                break
            await asyncio.sleep(0)
        assert len(results) == 2
        await asyncio.gather(*results)
        assert pair.rows() == [("a", "x"), ("b", "y")]
        assert pair.spy.tables == [[WRITES], [WRITES, CHECKPOINTS]]

    await with_pair(body)
    assert not unhandled


async def test_a_store_that_runs_after_the_handler_ended_fails_its_write_quietly(unhandled: list[dict[str, Any]]):
    backend = Backend()
    saver: list[SqlCheckpointSaver] = []
    writes: list[asyncio.Task[None]] = []

    async def handler(ctx: sessions.SessionContext) -> None:
        pair = Pair(ctx, backend)
        await pair.start()
        saver.append(pair.saver)
        await pair.saver._lock.acquire()  # the saver is busy: the write's rows wait for it, and the handler is over
        writes.append(pair.write("a", "x"))
        await iterations(5)

    await attempt(backend, handler)
    saver[0]._lock.release()
    with pytest.raises(RuntimeError, match="session is closed"):
        await writes[0]
    assert await stored_writes(dict(backend.stored)) == []
    assert not unhandled


# --- A run cancelled with a write pending ---------------------------------------------------------------------------


async def test_a_run_cancelled_with_a_write_pending_leaves_nothing_unhandled_and_the_next_attempt_resumes(
    unhandled: list[dict[str, Any]],
):
    backend = Backend()
    ran = {"x": 0, "y": 0, "join": 0}
    gate = asyncio.Event()
    running: list[asyncio.Task[Any]] = []
    failing: list[Spy] = []

    class Cancelling(SqlCheckpointSaver):
        """Cancels the run in the iteration after x's writes are handed over."""

        async def aput_writes(
            self, config: RunnableConfig, writes: typing.Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
        ) -> None:
            if any(channel == "par" for channel, _ in writes) and not failing[0].commits:
                failing[0].commits.append([])  # once
                asyncio.get_running_loop().call_soon(running[0].cancel)
            await super().aput_writes(config, writes, task_id, task_path)

    async def first(ctx: sessions.SessionContext) -> None:
        spy = Spy(ctx.db)
        failing.append(spy)
        # The write that is pending when the cancellation arrives cannot be stored: nobody awaits it any more.
        spy.fail = lambda tables: tables == [WRITES]
        app = parallel(ran, gate).compile(checkpointer=Cancelling(spy.database))
        running.append(asyncio.create_task(app.ainvoke({"par": []}, THREAD, durability="sync")))
        with pytest.raises(asyncio.CancelledError):
            await running[0]

    await attempt(backend, first)
    await iterations(100)  # the handler is over: a late hop would run now
    gc.collect()
    assert not unhandled
    gate.set()

    result: dict[str, Any] = {}

    async def second(ctx: sessions.SessionContext) -> None:
        app = parallel(ran).compile(checkpointer=SqlCheckpointSaver(ctx.db))
        result.update(await app.ainvoke(None, THREAD, durability="sync"))

    await attempt(backend, second)
    assert sorted(result["par"]) == ["x", "y"]
    assert ran["join"] == 1
    assert 1 <= ran["x"] <= 2
