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
        await conversation.append_reply({"role": "assistant", "text": "hello"})
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
        await conversation.append_reply({"role": "assistant", "text": "", "calls": calls})

        async def result(call_id: str) -> None:
            await conversation.append({"result": call_id, "output": f"{call_id} done"})

        await asyncio.gather(result("c1"), result("c2"))
        await conversation.append_reply({"role": "assistant", "text": "both"})
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
            await conversation.append_reply({"role": "assistant", "text": "", "calls": [call]})
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
    assert conversation.entries[-1].item == {"role": "assistant", "text": CANCELLED}
    # The run, unwinding after the stop, is fenced off.
    assert conversation.closed
    with pytest.raises(RuntimeError, match="stores nothing more"):
        await conversation.append({"role": "assistant", "text": "too late"})
    with pytest.raises(RuntimeError, match="stores nothing more"):
        await conversation.drop(conversation.entries[-1].n)
    [prompt] = [entry for entry in conversation.entries if entry.prompt is not None]
    with pytest.raises(RuntimeError, match="stores nothing more"):
        await conversation.answered(typing.cast(str, prompt.prompt), "too late")


async def test_a_dropped_reply_takes_what_followed_it_and_each_item_is_read_once():
    reads: list[Item] = []

    class Counting(Items):
        def read(self, item: Item) -> Parsed:
            reads.append(item)
            return super().read(item)

    async def run(conversation: Conversation) -> str:
        call = {"id": "c1", "name": "lookup", "input": {}}
        reply = await conversation.append_reply({"role": "assistant", "text": "", "calls": [call]})
        await conversation.append({"result": "c1", "output": "found"})
        assert conversation.has_result("c1") and not conversation.unanswered()
        await conversation.drop(reply)
        # The reply and the result made from it are gone, from memory and from the database alike.
        assert not conversation.has_result("c1")
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
    assert reads == [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": "", "calls": [{"id": "c1", "name": "lookup", "input": {}}]},
        {"result": "c1", "output": "found"},
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
    first = await conversation.append_reply(_calling("c1"), {"role": "assistant", "text": "thinking"}, agent="a")
    await conversation.append({"result": "c1", "output": "done"})
    second = await conversation.append_reply({"role": "assistant", "text": "there"}, agent="b")

    assert [e.n for e in conversation.reply_entries(first)] == [1, 2]
    assert [e.n for e in conversation.reply_entries(second)] == [4]
    assert conversation.reply_entries(3) == []
    assert (conversation.agent_of(first), conversation.agent_of(second), conversation.agent_of(3)) == ("a", "b", None)
    assert [e.n for e in conversation.after(0)] == [1, 2, 3, 4]
    assert conversation.after(4) == []
    assert conversation.history() == [e.item for e in conversation.entries]
    assert conversation.final_reply() == "there"


async def test_unanswered_calls_follow_appends_and_drops():
    conversation = _conversation({"role": "user", "text": "hi"})
    first = await conversation.append_reply(_calling("c1", "c2"))
    assert [c.id for c in conversation.unanswered()] == ["c1", "c2"]
    await conversation.append({"result": "c2", "output": "done"})
    assert [c.id for c in conversation.unanswered()] == ["c1"]
    await conversation.append({"result": "c1", "output": "done"})
    second = await conversation.append_reply(_calling("c3"))
    assert [c.id for c in conversation.unanswered()] == ["c3"]

    # Dropping the second reply forgets its call; dropping the first forgets its calls' results too.
    await conversation.drop(second)
    assert conversation.unanswered() == []
    assert conversation.has_result("c1")
    await conversation.drop(first)
    assert conversation.unanswered() == []
    assert not conversation.has_result("c1")
    assert [e.n for e in conversation.entries] == [0]
    # The positions are given again.
    assert await conversation.append_reply(_calling("c1")) == first
    assert [c.id for c in conversation.unanswered()] == ["c1"]


async def test_a_sealed_conversation_stores_nothing_more():
    conversation = _conversation({"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"})
    db = typing.cast(_Recorded, conversation.db)
    await conversation.seal()
    assert conversation.closed
    with pytest.raises(RuntimeError):
        await conversation.append({"role": "assistant", "text": "late"})
    with pytest.raises(RuntimeError):
        await conversation.append_reply({"role": "assistant", "text": "late"})
    with pytest.raises(RuntimeError):
        await conversation.drop(1)
    assert db.statements == []
    assert len(conversation.entries) == 2


async def test_answered_needs_a_prompt_of_the_conversation():
    conversation = _conversation()
    with pytest.raises(RuntimeError):
        await conversation.append({"role": "assistant", "text": "no turn"})
    await conversation.ask("p1", "hi")
    await conversation.answered("p1", "hello")
    assert conversation.answer_of("p1") == "hello"
    with pytest.raises(KeyError):
        await conversation.answered("p2", "hello")
    await conversation.seal()
    with pytest.raises(RuntimeError):
        await conversation.answered("p1", "again")
    assert conversation.answer_of("p1") == "hello"


async def test_a_call_id_used_twice_is_logged_once(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING, logger="restate.agents._conversation"):
        conversation = _conversation(_calling("c1"), {"result": "c1", "output": "done"})
        assert not caplog.records
        await conversation.append_reply(_calling("c1"))
        await conversation.append_reply(_calling("c1", "c1"))
    assert len(caplog.records) == 1
    assert "'c1'" in caplog.records[0].getMessage()


async def test_calls_that_share_an_id_are_each_unanswered_until_a_result_has_it():
    # Calls without an id are read with the id "": each still needs a result of its own when the run is closed.
    conversation = _conversation({"role": "user", "text": "hi"}, _calling("", ""))
    assert [c.id for c in conversation.unanswered()] == ["", ""]
    await conversation.append({"result": "", "output": "done"})
    assert conversation.unanswered() == []


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
        prompt = typing.cast(Entry, conversation.running())
        await conversation.append_reply(_calling(f"c{prompt.n}"))
        await conversation.append({"result": f"c{prompt.n}", "output": "done"})
        await conversation.append_reply({"role": "assistant", "text": f"answer {prompt.item['text']}"})
        return f"answer {prompt.item['text']}"

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

    transcript = [Entry(n, {"role": "user", "text": f"q{n}"}, f"p{n}", answer) for n, answer in enumerate(answers)]
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

    transcript = [Entry(2, {"role": "user", "text": "q2"}, "p2", "c")]
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
            await conversation.append_reply(_calling("c1"), agent="refunds")
            raise RuntimeError("the invocation is gone")
        return "fine"

    ask = asking(run)
    with pytest.raises(RuntimeError):
        await ask("first")
    # Another invocation: the first turn's is gone, so its turn ends, its call with a result, and it is not run again.
    assert await ask("second") == "fine"
    first, second = seen[1].transcript
    assert (first.answer, first.agent, second.prompt) == (ENDED, "refunds", second.prompt)
    assert seen[1].unanswered() == [] and len(seen) == 2


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
    # The last turn read the previous turn, the 5 before it that the budget holds, and the one past it: not all 99.
    assert items.loaded - loaded == 1 + 5 + 1
    assert [item["text"] for item in seen[-1]] == [
        *(text for n in range(93, 98) for text in (f"q{n}", "fine")),
        "q98",
        "q99",
    ]


async def test_a_turn_left_without_an_answer_gets_the_outcomes_of_its_calls():
    from restate.agents._calls import call_of  # pylint: disable=import-outside-toplevel
    from restate.agents._common import RETURNED_UNRECORDED  # pylint: disable=import-outside-toplevel

    seen: list[Conversation] = []

    async def run(conversation: Conversation) -> str:
        seen.append(conversation)
        if len(seen) == 1:
            await conversation.append_reply(_calling("c1", "c2"))
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
        await conversation.append_reply(_calling("c1", "c2"))
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
        await conversation.append_reply(_calling(f"c{len(seen)}"))
        await conversation.append({"result": f"c{len(seen)}", "output": "done"})
        return "fine"

    obj = sessions.SessionObject("asker")

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, ITEMS, run, window="all")

    backend = Backend()
    for n in range(4):
        await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), request(), f"q{n}")
    # Every turn is whole: its prompt, its call and its result (the last turn's too, appended since).
    assert len(seen[-1].entries) == 3 * 4 and len(seen[-1].history()) == len(seen[-1].entries)
