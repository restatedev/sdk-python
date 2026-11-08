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
"""The conversation every framework integration shares, with items of a made-up framework: plain dicts."""

# pylint: disable=missing-function-docstring,import-error

import asyncio
import json
import logging
import typing
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest
from sessions_database import Backend
from sessions_object import Host, request

import restate
from restate import sessions
from restate.agents._conversation import Call, Conversation, Entry, Item, Parsed, Result, answer_once
from restate.agents.internal import CANCELLED, from_tool, refusal_of
from restate.sessions.internal import invoke


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

PUBLISHED = {"publish": b"null"}
"""What the signals object answers when the turn publishes its progress."""


class Items:
    """A made-up framework's items: ``{"role", "text"}`` messages, a reply's ``calls``, and ``{"result", "output"}``."""

    def dump(self, item: Item) -> str:
        return json.dumps(item)

    def load(self, stored: str) -> Item:
        return json.loads(stored)

    def prompt(self, text: str) -> Item:
        return {"role": "user", "text": text}

    def reply(self, text: str, agent: str | None = None) -> Item:
        return {"role": "assistant", "text": text}

    def results(self, results: Sequence[tuple[Call, str]]) -> list[Item]:
        return [{"result": call.id, "output": text} for call, text in results]

    def read(self, item: Item) -> Parsed:
        return Parsed(
            (item["role"], item["text"]) if "role" in item else None,
            [Call(c["id"], c["name"], c["input"]) for c in item.get("calls", [])],
            [Result(item["result"], item["output"])] if "result" in item else [],
        )


ITEMS = Items()
Run = Callable[[Conversation], Awaitable[str]]


def asking(run: Run) -> Callable[..., Awaitable[Any]]:
    """Return how to ask an object whose handler answers each prompt with ``run``, in one backend."""
    obj = sessions.SessionObject("asker")

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, ITEMS, run)

    backend = Backend()

    async def asked(text: str, invocation: Any = None) -> Any:
        return await invoke(
            obj, "ask", Host(backend, service="asker", answers=PUBLISHED), invocation or request(), text
        )

    return asked


async def test_a_prompt_is_answered_once_and_a_retry_returns_the_stored_answer():
    runs: list[str] = []

    async def run(conversation: Conversation) -> str:
        runs.append("ran")
        await conversation.turn.append_reply({"role": "assistant", "text": "hello"})
        return "hello"

    ask, invocation = asking(run), request()
    assert await ask("hi", invocation) == "hello"
    assert await ask("hi", invocation) == "hello"
    assert runs == ["ran"]


async def test_tool_calls_run_at_once_append_each_result_as_it_comes():
    seen: list[list[Item]] = []

    async def run(conversation: Conversation) -> str:
        seen.append([entry.item for entry in conversation.entries])
        if len(seen) > 1:
            return "fine"
        calls = [{"id": "c1", "name": "lookup", "input": {}}, {"id": "c2", "name": "lookup", "input": {}}]
        await conversation.turn.append_reply({"role": "assistant", "text": "", "calls": calls})

        async def result(call_id: str) -> None:
            await conversation.turn.append({"result": call_id, "output": f"{call_id} done"})

        await asyncio.gather(result("c1"), result("c2"))
        await conversation.turn.append_reply({"role": "assistant", "text": "both"})
        return "both"

    ask = asking(run)
    assert await ask("first") == "both"
    assert await ask("second") == "fine"
    # Each result is stored, at a place of its own, and the reply follows them.
    assert seen[1][-4:] == [
        {"result": "c1", "output": "c1 done"},
        {"result": "c2", "output": "c2 done"},
        {"role": "assistant", "text": "both"},
        {"role": "user", "text": "second"},
    ]


async def test_a_failed_run_is_closed_with_its_error_and_the_next_prompt_starts_afresh():
    seen: list[list[Item]] = []

    async def run(conversation: Conversation) -> str:
        seen.append([entry.item for entry in conversation.entries])
        if len(seen) == 1:
            call = {"id": "c1", "name": "lookup", "input": {}}
            await conversation.turn.append_reply({"role": "assistant", "text": "", "calls": [call]})
            raise restate.TerminalError("boom")
        return "fine"

    ask = asking(run)
    with pytest.raises(restate.TerminalError, match="boom"):
        await ask("first")
    assert await ask("second") == "fine"
    # The cut-off call got its result, and the run its reply, before the next prompt.
    assert seen[1][-3:] == [
        {"result": "c1", "output": "Error: boom"},
        {"role": "assistant", "text": "Error: boom"},
        {"role": "user", "text": "second"},
    ]


async def test_a_stopped_run_stores_nothing_more():
    kept: list[Conversation] = []

    async def run(conversation: Conversation) -> str:
        kept.append(conversation)
        raise restate.TerminalError("stopped", status_code=409)

    ask = asking(run)
    with pytest.raises(restate.TerminalError, match="stopped"):
        await ask("first")
    [conversation] = kept
    turn = conversation.turn
    assert turn.entries[-1].item == {"role": "assistant", "text": CANCELLED}
    assert (turn.status, turn.answer) == ("cancelled", CANCELLED)
    # The run, unwinding after the stop, is fenced off.
    assert conversation.closed
    with pytest.raises(RuntimeError, match="stores nothing more"):
        await turn.append({"role": "assistant", "text": "too late"})
    with pytest.raises(RuntimeError, match="stores nothing more"):
        await turn.drop(turn.entries[-1].n)
    with pytest.raises(RuntimeError, match="stores nothing more"):
        await turn.answered("too late")


async def test_a_dropped_reply_takes_what_followed_it_and_each_item_is_read_once():
    reads: list[Item] = []

    class Counting(Items):
        def read(self, item: Item) -> Parsed:
            reads.append(item)
            return super().read(item)

    async def run(conversation: Conversation) -> str:
        call = {"id": "c1", "name": "lookup", "input": {}}
        reply = await conversation.turn.append_reply({"role": "assistant", "text": "", "calls": [call]})
        await conversation.turn.append({"result": "c1", "output": "found"})
        assert conversation.turn.has_result("c1") and not conversation.turn.open_calls()
        await conversation.turn.drop(reply)
        # The reply and the result made from it are gone, from memory and from the database alike.
        assert not conversation.turn.has_result("c1")
        stored = await Conversation.load(conversation.db, ITEMS)
        assert [entry.item for entry in stored.entries] == [entry.item for entry in conversation.entries]
        assert [entry.item for entry in stored.entries] == [{"role": "user", "text": "hi"}]
        return "done"

    obj = sessions.SessionObject("asker")

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, Counting(), run)

    host = Host(Backend(), service="asker", answers=PUBLISHED)
    assert await invoke(obj, "ask", host, request(), "hi") == "done"
    # The prompt, the reply and the result, each read once as it was appended; the transcript reads none again.
    # The run answered with no reply left: its answer is the turn's reply, read as it was appended too.
    assert reads == [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": "", "calls": [{"id": "c1", "name": "lookup", "input": {}}]},
        {"result": "c1", "output": "found"},
        {"role": "assistant", "text": "done"},
    ]


ANTHROPIC_NO_KEY = "Could not resolve authentication method. Expected either api_key or auth_token to be set."
"""How Anthropic's SDK says that its API key is missing: with a TypeError, of a built-in type."""


async def test_a_tools_error_of_a_built_in_type_is_marked_and_is_no_refusal():
    """from_tool holds the errors it marks: one of a built-in type has no weak reference. A model's is a refusal."""
    obj = sessions.SessionObject("marker")
    seen: dict[str, Any] = {}

    @obj.handler
    async def mark(ctx: sessions.SessionContext, text: str) -> None:
        tools = TypeError(ANTHROPIC_NO_KEY)
        from_tool(ctx, tools)
        seen["tool"] = refusal_of(ctx, tools)
        seen["model"] = refusal_of(ctx, TypeError(ANTHROPIC_NO_KEY))

    await invoke(obj, "mark", Host(Backend(), service="marker"), request(), "go")
    assert seen["tool"] is None
    assert seen["model"] is not None and seen["model"].status_code == 424


# --- The conversation's own bookkeeping, on a database that only records what it is asked ---------------------------


class _Recorded:
    """A database that records the SQL it is asked to run, and runs none of it."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []

    async def execute(self, sql: str, params: Any = None) -> None:
        self.statements.append((sql, params))

    def transaction(self) -> Any:
        recorded = self

        class _Tx:
            async def __aenter__(self) -> Any:
                return recorded

            async def __aexit__(self, *_: Any) -> None:
                return None

        return _Tx()


def _conversation(*items: Item) -> Conversation:
    # The first item starts a turn, as a prompt.
    entries = [Entry(n, item, prompt="p0" if n == 0 else None) for n, item in enumerate(items)]
    return Conversation(typing.cast(Any, _Recorded()), ITEMS, entries)


def _calling(*ids: str) -> Item:
    return {"role": "assistant", "text": "", "calls": [{"id": i, "name": "lookup", "input": {}} for i in ids]}


async def test_a_reply_and_what_follows_a_prompt_are_read_by_position():
    conversation = _conversation({"role": "user", "text": "hi"})
    first = await conversation.turn.append_reply(_calling("c1"), {"role": "assistant", "text": "thinking"}, agent="a")
    await conversation.turn.append({"result": "c1", "output": "done"})
    second = await conversation.turn.append_reply({"role": "assistant", "text": "there"}, agent="b")

    turn = conversation.turn
    assert [e.n for e in turn.reply_entries(first)] == [1, 2]
    assert [e.n for e in turn.reply_entries(second)] == [4]
    assert turn.reply_entries(3) == []
    assert (turn.agent_of(first), turn.agent_of(second), turn.agent_of(3)) == ("a", "b", None)
    assert [e.n for e in turn.items] == [1, 2, 3, 4]
    assert turn.replies() == [first, second]
    assert conversation.history() == [e.item for e in conversation.entries]
    assert turn.final_reply() == "there"


async def test_unanswered_calls_follow_appends_and_drops():
    conversation = _conversation({"role": "user", "text": "hi"})
    first = await conversation.turn.append_reply(_calling("c1", "c2"))
    assert [c.id for c in conversation.turn.open_calls()] == ["c1", "c2"]
    await conversation.turn.append({"result": "c2", "output": "done"})
    assert [c.id for c in conversation.turn.open_calls()] == ["c1"]
    await conversation.turn.append({"result": "c1", "output": "done"})
    second = await conversation.turn.append_reply(_calling("c3"))
    assert [c.id for c in conversation.turn.open_calls()] == ["c3"]

    # Dropping the second reply forgets its call; dropping the first forgets its calls' results too.
    await conversation.turn.drop(second)
    assert conversation.turn.open_calls() == []
    assert conversation.turn.has_result("c1")
    await conversation.turn.drop(first)
    assert conversation.turn.open_calls() == []
    assert not conversation.turn.has_result("c1")
    assert [e.n for e in conversation.entries] == [0]
    # The positions are given again.
    assert await conversation.turn.append_reply(_calling("c1")) == first
    assert [c.id for c in conversation.turn.open_calls()] == ["c1"]


async def test_a_sealed_conversation_stores_nothing_more():
    conversation = _conversation({"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"})
    db = typing.cast(_Recorded, conversation.db)
    await conversation.seal()
    assert conversation.closed
    with pytest.raises(RuntimeError):
        await conversation.turn.append({"role": "assistant", "text": "late"})
    with pytest.raises(RuntimeError):
        await conversation.turn.append_reply({"role": "assistant", "text": "late"})
    with pytest.raises(RuntimeError):
        await conversation.turn.drop(1)
    assert db.statements == []
    assert len(conversation.entries) == 2


async def test_only_the_turn_being_answered_is_written_and_it_is_answered_once():
    conversation = _conversation()
    with pytest.raises(RuntimeError, match="no turn"):
        await conversation.turn.append({"role": "assistant", "text": "no turn"})
    first = await conversation.ask("p1", "hi")
    with pytest.raises(RuntimeError, match="never overlap"):
        await conversation.ask("p2", "hello")
    await first.answered("hello")
    assert conversation.answer_of("p1") == "hello" and first.status == "answered"
    # The answer, with no reply of the model, is the turn's reply: the message the summary shows.
    assert first.items[-1].item == {"role": "assistant", "text": "hello"}
    assert first.prompt.answered_at == first.items[-1].n
    with pytest.raises(RuntimeError, match="not the turn being answered"):
        await first.answered("again")
    second = await conversation.ask("p2", "and you?")
    # An earlier turn is read, never written.
    with pytest.raises(RuntimeError, match="not the turn being answered"):
        await first.append({"role": "assistant", "text": "late"})
    await conversation.seal()
    with pytest.raises(RuntimeError, match="stores nothing more"):
        await second.answered("again")
    assert conversation.answer_of("p1") == "hello"


async def test_a_call_id_used_twice_is_logged_once(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="restate.agents._conversation"):
        conversation = _conversation(_calling("c1"), {"result": "c1", "output": "done"})
        assert not caplog.records
        await conversation.turn.append_reply(_calling("c1"))
        await conversation.turn.append_reply(_calling("c1", "c1"))
    assert len(caplog.records) == 1
    assert "'c1'" in caplog.records[0].getMessage()


async def test_calls_that_share_an_id_are_each_unanswered_until_a_result_has_it():
    # Calls without an id are read with the id "": each still needs a result of its own when the run is closed.
    conversation = _conversation({"role": "user", "text": "hi"}, _calling("", ""))
    assert [c.id for c in conversation.turn.open_calls()] == ["", ""]
    await conversation.turn.append({"result": "", "output": "done"})
    assert conversation.turn.open_calls() == []


# --- Turns: what a turn loads, and what the model sees ---------------------------------------------------------------


class _Counting(Items):
    """The made-up framework's items, counting how many it loads from the database."""

    def __init__(self) -> None:
        self.loaded = 0

    def load(self, stored: str) -> Item:
        self.loaded += 1
        return super().load(stored)


async def test_a_turn_loads_the_transcript_and_only_the_latest_turns_whole():
    items = _Counting()
    obj = sessions.SessionObject("asker")
    seen: list[list[Item]] = []

    async def run(conversation: Conversation) -> str:
        seen.append(conversation.history())
        turn = conversation.turn
        await turn.append_reply(_calling(f"c{turn.n}"))
        await turn.append({"result": f"c{turn.n}", "output": "done"})
        await turn.append_reply({"role": "assistant", "text": f"answer {turn.prompt.item['text']}"})
        return f"answer {turn.prompt.item['text']}"

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, items, run)

    backend = Backend()
    loaded = 0
    for n in range(30):
        loaded = items.loaded
        await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), request(), f"q{n}")
    # The last turn loaded each earlier turn's prompt once, and the items of one turn only: the previous one's 3.
    assert items.loaded - loaded == 29 + 3
    # The model saw the turns before the previous one as their messages and answers, then the previous one whole.
    *earlier, previous, previous_reply, previous_result, previous_answer, prompt = seen[-1]
    assert len(earlier) == 28 * 2 and previous == {"role": "user", "text": "q28"}
    assert earlier[:2] == [{"role": "user", "text": "q0"}, {"role": "assistant", "text": "answer q0"}]
    assert [item for item in earlier if "calls" in item or "result" in item] == []
    assert previous_reply["calls"] and previous_result["result"] and previous_answer["text"] == "answer q28"
    assert prompt == {"role": "user", "text": "q29"}


def _turns(*answers: str) -> Conversation:
    """A conversation of answered turns, each its prompt and answer, loaded with no turn whole."""

    transcript = [
        Entry(n, {"role": "user", "text": f"q{n}"}, f"p{n}", answer, status="answered")
        for n, answer in enumerate(answers)
    ]
    return Conversation(typing.cast(Any, _Recorded()), ITEMS, [], transcript=transcript, budget=10, window=0)


async def test_the_model_sees_the_latest_earlier_turns_that_the_budget_holds():
    # 40 characters: the turns of 2 + 18 characters each, of which two fit.
    conversation = _turns("a" * 18, "b" * 18, "c" * 18)
    assert conversation.history() == [
        {"role": "user", "text": "q1"},
        {"role": "assistant", "text": "b" * 18},
        {"role": "user", "text": "q2"},
        {"role": "assistant", "text": "c" * 18},
    ]
    # They outgrow it: the turns that half of it does not hold are summarized, up to the one it does.
    assert conversation.overflowing()
    found = conversation.to_summarize()
    assert found is not None and [turn.n for turn in found[0]] == [0, 1] and found[1] == 2


async def test_a_summary_stands_for_the_turns_before_the_transcript():
    from restate.agents._conversation import SUMMARY, Start  # pylint: disable=import-outside-toplevel

    transcript = [Entry(2, {"role": "user", "text": "q2"}, "p2", "c", status="answered")]
    start = Start(summary="they talked", summarized=2)
    conversation = Conversation(typing.cast(Any, _Recorded()), ITEMS, [], start, transcript=transcript, window=0)
    assert conversation.history() == [
        {"role": "user", "text": SUMMARY + "they talked"},
        {"role": "assistant", "text": "Understood."},
        {"role": "user", "text": "q2"},
        {"role": "assistant", "text": "c"},
    ]


async def test_a_turn_left_without_an_answer_is_ended_by_the_next_and_carries_on_its_agent():
    from restate.agents.internal import ENDED  # pylint: disable=import-outside-toplevel

    seen: list[Conversation] = []

    async def run(conversation: Conversation) -> str:
        seen.append(conversation)
        if len(seen) == 1:
            await conversation.turn.append_reply(_calling("c1"), agent="refunds")
            raise RuntimeError("the invocation is gone")
        return "fine"

    ask = asking(run)
    with pytest.raises(RuntimeError):
        await ask("first")
    # Another invocation: the first turn's is gone, so its turn ends, its call with a result, and it is not run again.
    assert await ask("second") == "fine"
    first, second = seen[1].transcript
    assert (first.answer, first.agent, second.prompt) == (ENDED, "refunds", second.prompt)
    assert seen[1].turn.open_calls() == [] and len(seen) == 2


async def test_a_turn_without_a_summary_reads_only_the_turns_its_budget_holds():
    items = _Counting()
    obj = sessions.SessionObject("asker")
    seen: list[list[Item]] = []

    async def run(conversation: Conversation) -> str:
        seen.append(conversation.history())
        return "fine"

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        # 10 tokens: 40 characters of turns before the window, of 3 + 4 characters each.
        return await answer_once(ctx, text, items, run, budget=10)

    backend = Backend()
    loaded = 0
    for n in range(100):
        loaded = items.loaded
        await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), request(), f"q{n:02}")
    # The last turn read the previous turn (its prompt and answer), the 5 before it that the budget holds, and the
    # one past it: not all 99.
    assert items.loaded - loaded == 2 + 5 + 1
    assert [item["text"] for item in seen[-1]] == [
        *(text for n in range(93, 99) for text in (f"q{n}", "fine")),
        "q99",
    ]


async def test_a_turn_left_without_an_answer_gets_the_outcomes_of_its_calls():
    from restate.agents._calls import call_of  # pylint: disable=import-outside-toplevel
    from restate.agents._common import RETURNED_UNRECORDED  # pylint: disable=import-outside-toplevel

    seen: list[Conversation] = []

    async def run(conversation: Conversation) -> str:
        seen.append(conversation)
        if len(seen) == 1:
            await conversation.turn.append_reply(_calling("c1", "c2"))
            ctx = typing.cast(Any, conversation).ctx
            # c1's Restate call returned, but the invocation was gone before its result was stored; c2's never did.
            await (await call_of(ctx, "lookup", {}, "c1")).answered()
            raise RuntimeError("the invocation is gone")
        return "fine"

    obj = sessions.SessionObject("asker")

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        async def keeping(conversation: Conversation) -> str:
            typing.cast(Any, conversation).ctx = ctx
            return await run(conversation)

        return await answer_once(ctx, text, ITEMS, keeping)

    backend = Backend()
    with pytest.raises(RuntimeError):
        await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), request(), "first")
    await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), request(), "second")
    results = {r.id: r.output for e in seen[1].entries for r in e.results}
    assert results["c1"] == RETURNED_UNRECORDED and results["c2"] != RETURNED_UNRECORDED


async def test_settle_closes_only_the_turn_its_gone_invocation_left_and_only_once():
    from restate.agents._calls import call_of  # pylint: disable=import-outside-toplevel
    from restate.agents._common import ENDED, RETURNED_UNRECORDED  # pylint: disable=import-outside-toplevel
    from restate.agents._conversation import settle  # pylint: disable=import-outside-toplevel

    obj = sessions.SessionObject("asker")

    async def dying(conversation: Conversation) -> str:
        await conversation.turn.append_reply(_calling("c1", "c2"))
        # c1's Restate call returned, but the invocation was gone before its result was stored; c2's never did.
        await (await call_of(typing.cast(Any, conversation).ctx, "lookup", {}, "c1")).answered()
        raise RuntimeError("the invocation is gone")

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        async def run(conversation: Conversation) -> str:
            typing.cast(Any, conversation).ctx = ctx
            return await dying(conversation)

        return await answer_once(ctx, text, ITEMS, run)

    @obj.handler(name="settle")
    async def settling(ctx: sessions.SessionContext, invocation_id: str) -> bool:
        return await settle(ctx, ITEMS, invocation_id)

    @obj.handler
    async def read(ctx: sessions.SessionContext) -> list[Any]:
        conversation = await Conversation.load(ctx.db, ITEMS)
        return [(e.prompt, e.answer, [(r.id, r.output) for r in e.results]) for e in conversation.entries]

    backend = Backend()

    def host() -> Host:
        return Host(backend, service="asker", answers=PUBLISHED)

    gone = request()
    with pytest.raises(RuntimeError):
        await invoke(obj, "ask", host(), gone, "first")
    # Another invocation's id closes nothing; the gone one's closes its turn, with its calls' outcomes, once.
    assert await invoke(obj, "settle", host(), request(), "inv_other") is False
    assert await invoke(obj, "settle", host(), request(), gone.id) is True
    assert await invoke(obj, "settle", host(), request(), gone.id) is False
    entries = await invoke(obj, "read", host(), request())
    assert tuple(entries[0][:2]) == (gone.id, ENDED)
    results = dict(output for e in entries for output in e[2])
    assert results == {"c1": RETURNED_UNRECORDED, "c2": ENDED}


async def test_a_window_of_all_loads_every_turn_whole():
    seen: list[Conversation] = []

    async def run(conversation: Conversation) -> str:
        seen.append(conversation)
        await conversation.turn.append_reply(_calling(f"c{len(seen)}"))
        await conversation.turn.append({"result": f"c{len(seen)}", "output": "done"})
        return "fine"

    obj = sessions.SessionObject("asker")

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, ITEMS, run, window="all")

    backend = Backend()
    for n in range(4):
        await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), request(), f"q{n}")
    # Every turn is whole: its prompt, its call, its result and its answer, which it ends with (the last turn's too,
    # appended since).
    assert len(seen[-1].entries) == 4 * 4 and len(seen[-1].history()) == len(seen[-1].entries)


def test_a_turn_another_invocation_ended_stays_in_the_window_as_an_unanswered_one_does():
    from restate.agents._conversation import Status, window_start  # pylint: disable=import-outside-toplevel

    def turns(*statuses: Status | None) -> list[Entry]:
        return [
            Entry(n, {"role": "user", "text": f"q{n}"}, f"p{n}", status=status) for n, status in enumerate(statuses)
        ]

    # Its framework never finished it, so what it carried is a guess: the next turn reads it whole.
    assert window_start(turns("answered", "ended"), 0) == 1
    assert window_start(turns("answered", "ended", "ended"), 0) == 1
    assert window_start(turns("answered", "answered", "ended"), 1) == 1
    assert window_start(turns("answered", "ended", None), 0) == 1
    # One before the window's answered turns is not: the turns after it carried on from it.
    assert window_start(turns("answered", "ended", "answered"), 1) == 2
    assert window_start(turns("answered", "ended", "answered"), 0) == 3
    # A turn closed by its own run (stopped, failed, refused) carried what its framework had: it counts as answered.
    assert window_start(turns("answered", "cancelled", None), 0) == 2
    # Otherwise, as before.
    assert window_start(turns("answered", "answered", None), 1) == 1
    assert window_start(turns("answered", "answered"), 0) == 2


async def test_a_turns_open_calls_are_its_own_and_not_an_earlier_turns():
    entries = [
        Entry(0, {"role": "user", "text": "first"}, "p0", "done", status="answered"),
        Entry(1, _calling("c0")),
        Entry(2, {"role": "user", "text": "second"}, "p1"),
        Entry(3, _calling("c1")),
    ]
    conversation = Conversation(typing.cast(Any, _Recorded()), ITEMS, entries)
    first, second = conversation.turns
    # The first turn ended with a call left open: it is that turn's, and closing the second does not answer it.
    assert [c.id for c in first.open_calls()] == ["c0"]
    assert [c.id for c in second.open_calls()] == ["c1"]
    assert conversation.turn is second and conversation.running() is second
    # The model was given both turns, flat: the one place where turns are composed.
    assert conversation.history() == [e.item for e in entries]
    assert conversation.knows_result("c0") is False and [e.turn for e in conversation.entries] == [0, 0, 2, 2]


async def test_a_turn_answered_with_nothing_to_say_is_answered_once():
    class Silent(Items):
        """Items that read an empty message as no message, as ADK's do."""

        def read(self, item: Item) -> Parsed:
            parsed = super().read(item)
            return parsed._replace(said=None) if parsed.said is not None and not parsed.said[1] else parsed

    silent = Silent()
    conversation = Conversation(typing.cast(Any, _Recorded()), silent, [])
    turn = await conversation.ask("p1", "do it quietly")
    # No message of the assistant, and an empty answer: nothing to show as the answer, and nothing stored for it.
    await turn.answered("")
    assert (turn.status, turn.answer, turn.prompt.answered_at) == ("answered", "", None)
    assert turn.items == []


async def test_a_turn_asks_again_within_itself_once_across_attempts():
    replies = iter(["prose", "crash", "still prose"])
    seen: list[Conversation] = []

    async def run(conversation: Conversation) -> str:
        seen.append(conversation)
        reply = next(replies)
        if reply == "crash":
            raise RuntimeError("the process crashed after asking again")
        await conversation.turn.append_reply({"role": "assistant", "text": reply})
        return reply

    def check(answer: str) -> str | None:
        return None if answer.startswith("{") else "Reply again, with JSON only."

    obj = sessions.SessionObject("asker")

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, ITEMS, run, check=check)

    backend, invocation = Backend(), request()
    with pytest.raises(RuntimeError, match="crashed"):
        await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), invocation, "plan")
    # The retry finds the ask again stored: it does not ask again a second time, and answers with the reply to it.
    assert (
        await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), invocation, "plan") == "still prose"
    )
    turn = seen[-1].turn
    assert len(seen[-1].transcript) == 1 and turn.answer == "still prose"
    assert [e.item for e in turn.items] == [
        {"role": "assistant", "text": "prose"},
        {"role": "user", "text": "Reply again, with JSON only."},
        {"role": "assistant", "text": "still prose"},
    ]
    assert turn.latest_ask().item == {"role": "user", "text": "Reply again, with JSON only."}


async def test_a_turn_that_ends_with_a_tool_after_a_message_ends_with_its_answer():
    conversation = _conversation({"role": "user", "text": "book Rome"})
    turn = conversation.turn
    # The model says something, then calls a tool, whose output is the answer (a tool that ends the turn).
    reply = {"role": "assistant", "text": "Let me book that.", "calls": [{"id": "b1", "name": "book", "input": {}}]}
    await turn.append_reply(reply, agent="booker")
    await turn.append({"result": "b1", "output": "booked Rome"})
    await turn.answered("booked Rome")
    # The turn ends with its answer, as a message of the assistant: the summary's, and what the next model sees.
    last = turn.items[-1]
    assert last.item == {"role": "assistant", "text": "booked Rome"} and last.agent == "booker"
    assert turn.prompt.answered_at == last.n
