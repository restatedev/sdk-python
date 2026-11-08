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
"""What the core gives the frameworks beyond the turn projection: snapshots, a time on each turn, and waits.

An agent that takes its history from snapshots gives the model the latest
snapshot (the history as its framework's own compaction left it), then every
row stored after it, whole. The items are the made-up framework's dicts of
``agents_conversation``.
"""

# pylint: disable=missing-function-docstring,import-error

import json
import time
from typing import Any

import pytest
from agents_conversation import ITEMS, PUBLISHED, Run
from sessions_database import Backend
from sessions_object import Host, request

from restate import sessions
from restate.agents import AgentObject, _messages
from restate.agents._common import CompactHistory
from restate.agents._conversation import (
    SCHEMA,
    TURNS,
    Conversation,
    Item,
    answer_once,
    compact,
    latest_snapshot,
    next_start,
    restart,
)
from restate.agents._turn import AGENT, AgentInfo
from restate.agents.internal import wait_for
from restate.sessions.internal import invoke, reset


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

SUMMARY = {"role": "user", "text": "<summary>q0 and q1 were asked and answered</summary>"}
"""What the made-up framework's compaction leaves in place of the history it compacted."""


def _calling(call_id: str) -> Item:
    return {"role": "assistant", "text": "", "calls": [{"id": call_id, "name": "lookup", "input": {}}]}


class _Agent:
    """An object whose handler answers each prompt with a run, taking its history from snapshots."""

    def __init__(self, run: Run, compact_history: CompactHistory | None = None) -> None:
        self.obj = sessions.SessionObject("snapshots")
        AGENT.on(self.obj, AgentInfo(snapshots=True, compact_history=compact_history))
        self.backend = Backend()
        self.hosts: list[Host] = []

        @self.obj.handler
        async def ask(ctx: sessions.SessionContext, text: str) -> str:
            return await answer_once(ctx, text, ITEMS, run)

        @self.obj.handler(name="compact")
        async def compacting(ctx: sessions.SessionContext, _: Any = None) -> bool:
            return await compact(ctx, ITEMS)

        @self.obj.handler
        async def read(ctx: sessions.SessionContext, _: Any = None) -> Any:
            snapshot = await latest_snapshot(ctx.db, ITEMS)
            rows = await ctx.db.values(f"SELECT prompt, asked_at FROM {TURNS} ORDER BY n")
            covered = None if snapshot is None else [snapshot.covers, snapshot.turn, list(snapshot.items)]
            return {"snapshot": covered, "turns": [list(row) for row in rows]}

        @self.obj.handler
        async def erase(ctx: sessions.SessionContext, _: Any = None) -> None:
            # As the agent's ``clear`` empties the key's database.
            await SCHEMA.ensure(ctx.db)
            start = await next_start(ctx.db)
            async with ctx.db.transaction() as tx:
                await reset(tx)
                await restart(tx, start)

        @self.obj.view
        async def messages(ctx: sessions.ViewContext, _: Any = None) -> Any:
            return await _messages.page(ctx.db, ITEMS, {})

    def host(self) -> Host:
        host = Host(self.backend, service="snapshots", answers=PUBLISHED)
        self.hosts.append(host)
        return host

    async def __call__(self, handler: str, arg: Any = None, invocation: Any = None) -> Any:
        return await invoke(self.obj, handler, self.host(), invocation or request(), arg)

    async def view(self, handler: str) -> Any:
        return await invoke(self.obj, handler, self.host().view(), request(), None)


class _Seen(list[Conversation]):
    """The conversations the runs got, and the history each gave the model at its start."""

    def __init__(self) -> None:
        super().__init__()
        self.histories: list[list[Item]] = []

    def add(self, conversation: Conversation) -> None:
        self.append(conversation)
        self.histories.append(conversation.history())


def _tools_then_answer(seen: _Seen, snapshot_in: int | None = None) -> Run:
    """A run that makes a tool call, stores its result, (in turn ``snapshot_in``, stores a snapshot), then answers."""

    async def run(conversation: Conversation) -> str:
        seen.add(conversation)
        turn = conversation.turn
        n = len(seen) - 1
        await turn.append_reply(_calling(f"c{n}"))
        await turn.append({"result": f"c{n}", "output": f"found {n}"})
        if n == snapshot_in:
            await turn.snapshot([SUMMARY, {"role": "user", "text": f"q{n}"}, {"result": f"c{n}", "output": "kept"}])
        await turn.append_reply({"role": "assistant", "text": f"answer {n}"})
        return f"answer {n}"

    return run


async def test_before_any_snapshot_the_model_gets_the_whole_conversation_whatever_the_window():
    seen = _Seen()
    agent = _Agent(_tools_then_answer(seen))
    for n in range(3):
        assert await agent("ask", f"q{n}") == f"answer {n}"
    # The default window is 1, but snapshots load every turn whole, with every tool call and result.
    history = seen.histories[-1]
    assert [item.get("result") for item in history if "result" in item] == ["c0", "c1"]
    assert history[0] == {"role": "user", "text": "q0"} and history[-1] == {"role": "user", "text": "q2"}
    assert seen[-1].summary is None and not seen[-1].overflowing()


async def test_a_run_takes_its_history_from_the_latest_snapshot_then_every_row_after_it():
    seen = _Seen()
    agent = _Agent(_tools_then_answer(seen, snapshot_in=1))
    for n in range(3):
        assert await agent("ask", f"q{n}") == f"answer {n}"
    latest = seen[-1]
    # Turn 0 is before the snapshot: it is not loaded whole. Turn 1, which stored it, is.
    assert [turn.prompt.item["text"] for turn in latest.turns] == ["q1", "q2"]
    assert seen.histories[-1] == [
        SUMMARY,
        {"role": "user", "text": "q1"},
        {"result": "c1", "output": "kept"},
        # Turn 1's answer came after the snapshot: it is a row after it.
        {"role": "assistant", "text": "answer 1"},
        {"role": "user", "text": "q2"},
    ]


async def test_a_snapshot_counts_from_the_next_request_of_the_run_that_stored_it():
    histories: list[list[Item]] = []

    async def run(conversation: Conversation) -> str:
        turn = conversation.turn
        await turn.append_reply(_calling("c0"))
        await turn.append({"result": "c0", "output": "found"})
        await turn.snapshot([SUMMARY])
        histories.append(conversation.history())
        await turn.append_reply({"role": "assistant", "text": "done"})
        histories.append(conversation.history())
        return "done"

    agent = _Agent(run)
    assert await agent("ask", "q0") == "done"
    assert histories == [[SUMMARY], [SUMMARY, {"role": "assistant", "text": "done"}]]


async def test_the_results_inside_a_snapshot_are_known_and_not_stored_again():
    async def compact_history(ctx: sessions.SessionContext, history: list[Any]) -> list[Any] | None:
        return [SUMMARY, {"result": "c0", "output": "kept"}]

    seen = _Seen()
    run = _tools_then_answer(seen)

    async def asking_to_compact(conversation: Conversation) -> str:
        answer = await run(conversation)
        conversation.compact_after()
        return answer

    agent = _Agent(asking_to_compact, compact_history)
    await agent("ask", "q0")
    assert await agent("compact") is True
    await agent("ask", "q1")
    later = seen[-1]
    # Turn 0 is not loaded: c0's result is only in the snapshot. c1's is in turn 1; c9's in neither.
    assert [turn.prompt.item["text"] for turn in later.turns] == ["q1"]
    assert later.knows_result("c0") and later.knows_result("c1") and not later.knows_result("c9")
    handed_back = [{"result": "c0", "output": "kept"}, {"result": "c9", "output": "new"}]
    assert later.new_results(handed_back) == [{"result": "c9", "output": "new"}]


async def test_an_attempt_that_crashes_after_a_snapshot_resumes_from_it():
    attempts: list[list[Item]] = []

    async def run(conversation: Conversation) -> str:
        attempts.append(conversation.history())
        turn = conversation.turn
        if len(attempts) == 1:
            await turn.append_reply(_calling("c0"))
            await turn.append({"result": "c0", "output": "found"})
            await turn.snapshot([SUMMARY, {"result": "c0", "output": "found"}])
            raise RuntimeError("the process crashed")
        await turn.append_reply({"role": "assistant", "text": "done"})
        return "done"

    agent, invocation = _Agent(run), request()
    with pytest.raises(RuntimeError, match="crashed"):
        await agent("ask", "q0", invocation)
    assert await agent("ask", "q0", invocation) == "done"
    # The retry carries on the same turn, from the snapshot: not from the rows it stands for.
    assert attempts[1] == [SUMMARY, {"result": "c0", "output": "found"}]
    stored = await agent("read")
    assert stored["snapshot"][:2] == [2, 0] and len(stored["turns"]) == 1


async def test_the_views_never_show_a_snapshot():
    seen = _Seen()
    agent = _Agent(_tools_then_answer(seen, snapshot_in=0))
    await agent("ask", "q0")
    page = await agent.view("messages")
    texts = [part.get("text") for message in page["messages"] for part in message["parts"]]
    assert "q0" in texts and "answer 0" in texts
    assert SUMMARY["text"] not in texts and [message["id"] for message in page["messages"]] == [0, 1, 3]


async def test_clear_drops_the_snapshots_with_everything_else():
    seen = _Seen()
    agent = _Agent(_tools_then_answer(seen, snapshot_in=0))
    await agent("ask", "q0")
    assert (await agent("read"))["snapshot"] is not None
    await agent("erase")
    assert (await agent("read"))["snapshot"] is None
    await agent("ask", "q1")
    assert seen.histories[-1] == [{"role": "user", "text": "q1"}]


async def test_a_turn_that_asks_for_it_is_compacted_after_its_answer_and_the_next_starts_from_it():
    compacted: list[list[Item]] = []

    async def compact_history(ctx: sessions.SessionContext, history: list[Any]) -> list[Any] | None:
        compacted.append(history)
        return [SUMMARY]

    seen = _Seen()

    async def run(conversation: Conversation) -> str:
        seen.add(conversation)
        await conversation.turn.append_reply({"role": "assistant", "text": f"answer {len(seen)}"})
        if len(seen) == 2:
            conversation.compact_after()
        return f"answer {len(seen)}"

    agent = _Agent(run, compact_history)
    await agent("ask", "q0")
    assert [send.handler for host in agent.hosts for send in host.sends] == []
    await agent("ask", "q1")
    [send] = agent.hosts[-1].sends
    assert send.handler == "compact" and send.key == "alice"
    assert send.idempotency_key is not None and send.idempotency_key.endswith(":compact")

    assert await agent("compact") is True
    assert compacted == [
        [
            {"role": "user", "text": "q0"},
            {"role": "assistant", "text": "answer 1"},
            {"role": "user", "text": "q1"},
            {"role": "assistant", "text": "answer 2"},
        ]
    ]
    # Stored between turns: it covers both turns, and no turn has it.
    assert (await agent("read"))["snapshot"] == [3, None, [SUMMARY]]
    await agent("ask", "q2")
    assert seen.histories[-1] == [SUMMARY, {"role": "user", "text": "q2"}]
    assert [turn.prompt.item["text"] for turn in seen[-1].turns] == ["q2"]


async def test_compact_leaves_alone_a_turn_left_without_an_answer():
    calls: list[list[Any]] = []

    async def compact_history(ctx: sessions.SessionContext, history: list[Any]) -> list[Any] | None:
        calls.append(history)
        return [SUMMARY]

    async def run(conversation: Conversation) -> str:
        await conversation.turn.append_reply(_calling("c0"))
        raise RuntimeError("the invocation is gone")

    agent = _Agent(run, compact_history)
    with pytest.raises(RuntimeError):
        await agent("ask", "q0")
    # The next message closes the turn first: compacting now would hide it from that message.
    assert await agent("compact") is False and calls == []


async def test_compact_stores_nothing_when_the_framework_leaves_the_history_as_it_is():
    async def compact_history(ctx: sessions.SessionContext, history: list[Any]) -> list[Any] | None:
        return None

    async def run(conversation: Conversation) -> str:
        await conversation.turn.append_reply({"role": "assistant", "text": "hi"})
        return "hi"

    agent = _Agent(run, compact_history)
    await agent("ask", "q0")
    assert await agent("compact") is False
    assert (await agent("read"))["snapshot"] is None


async def test_a_reply_a_snapshot_covers_cannot_be_dropped():
    async def run(conversation: Conversation) -> str:
        turn = conversation.turn
        reply = await turn.append_reply(_calling("c0"))
        await turn.snapshot([SUMMARY])
        with pytest.raises(RuntimeError, match="snapshot"):
            await turn.drop(reply)
        return "kept"

    assert await _Agent(run)("ask", "q0") == "kept"


def test_compact_history_needs_snapshots():
    agent = AgentObject("needs_snapshots")

    async def answer(ctx: sessions.SessionContext, text: str) -> str:
        return text

    async def compact_history(ctx: sessions.SessionContext, history: list[Any]) -> list[Any] | None:
        return None

    with pytest.raises(ValueError, match="snapshots=True"):
        agent.chat(answer, items=ITEMS, compact_history=compact_history)
    agent.chat(answer, items=ITEMS, snapshots=True, compact_history=compact_history)


async def test_each_turns_row_has_the_time_it_was_asked():
    async def run(conversation: Conversation) -> str:
        return "fine"

    agent = _Agent(run)
    before = int(time.time() * 1000)
    await agent("ask", "q0")
    after = int(time.time() * 1000)
    [[_, asked_at]] = (await agent("read"))["turns"]
    assert before <= asked_at <= after


async def test_a_framework_waits_for_a_signal_for_its_own_tool_call_once():
    obj = sessions.SessionObject("waiter")

    @obj.handler
    async def deferred(ctx: sessions.SessionContext, call_id: str) -> Any:
        return await wait_for(ctx, call_id, "approval", {"refund": 5})

    backend, invocation = Backend(), request()

    def host() -> Host:
        return Host(backend, service="waiter", key="alice", answers={"wait": b'"approved"'})

    first = host()
    assert await invoke(obj, "deferred", first, invocation, "call-1") == "approved"
    [(service, handler, arg, key, idempotency_key)] = first.calls
    assert (service, handler, key, idempotency_key) == ("signals", "wait", "waiter:alice", f"{invocation.id}:call-1")
    assert json.loads(arg) == {"id": f"{invocation.id}:call-1", "name": "approval", "input": {"refund": 5}}
    # The model made the same call again under a new id (after a crash, say): it joins the same wait.
    again = host()
    assert await invoke(obj, "deferred", again, invocation, "call-2") == "approved"
    assert [call[4] for call in again.calls] == [f"{invocation.id}:call-1"]


async def test_a_wait_with_nothing_to_show_shows_no_input():
    obj = sessions.SessionObject("waiter")

    @obj.handler
    async def deferred(ctx: sessions.SessionContext, call_id: str) -> Any:
        return await wait_for(ctx, call_id, "approval")

    host = Host(Backend(), service="waiter", key="alice", answers={"wait": b"true"})
    assert await invoke(obj, "deferred", host, request(), "call-1") is True
    [(_, _, arg, _, _)] = host.calls
    assert "input" not in json.loads(arg)
