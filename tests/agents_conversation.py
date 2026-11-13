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
import random
import typing
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest
from sessions_database import Backend
from sessions_object import Host, request

import restate
from restate import sessions
from restate.agents._conversation import (
    SCHEMA,
    STEPS,
    TURNS,
    Call,
    Conversation,
    Entry,
    Item,
    Parsed,
    Result,
    answer_once,
    last_of,
)
from restate.agents._common import Check
from restate.agents._signals import with_companion
from restate.agents.internal import CANCELLED, from_tool, refusal_of
from restate.sessions.internal import invoke


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

REVISION = b'{"revision": 1}'
"""What the agent's companion answers a turn's ``start`` and each ``publish`` with: the revision."""

PUBLISHED = {"publish": REVISION}
"""What the agent's companion answers when the turn publishes its progress."""


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
    return asking_with(run, None)


def asking_with(run: Run, check: Check | None) -> Callable[..., Awaitable[Any]]:
    """Return how to ask an object whose handler answers each prompt with ``run``, checked with ``check``."""
    obj = sessions.SessionObject("asker")

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, ITEMS, run, check=check)

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


async def test_a_409_that_is_no_cancellation_fails_the_turn_rather_than_cancelling_it():
    """A call that fails with 409 for another reason (a workflow run a second time) fails the turn with its error."""
    seen: list[list[Item]] = []

    async def run(conversation: Conversation) -> str:
        seen.append([entry.item for entry in conversation.entries])
        if len(seen) == 1:
            raise restate.TerminalError("the workflow method was already invoked", status_code=409)
        return "fine"

    ask = asking(run)
    with pytest.raises(restate.TerminalError, match="already invoked"):
        await ask("first")
    assert await ask("second") == "fine"
    assert seen[1][-2:] == [
        {"role": "assistant", "text": "Error: the workflow method was already invoked"},
        {"role": "user", "text": "second"},
    ]


async def test_a_stopped_run_stores_nothing_more():
    kept: list[Conversation] = []

    async def run(conversation: Conversation) -> str:
        kept.append(conversation)
        raise restate.TerminalError("cancelled", status_code=409)

    ask = asking(run)
    with pytest.raises(restate.TerminalError, match="cancelled"):
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


async def test_a_retry_of_a_closed_turn_fails_as_the_prompt_did():
    from restate.agents.internal import ENDED  # pylint: disable=import-outside-toplevel

    async def run(conversation: Conversation) -> str:
        text = conversation.turn.prompt.item["text"]
        if text == "boom":
            raise restate.TerminalError("boom")
        if text == "stop":
            raise restate.TerminalError("cancelled", status_code=409)
        await conversation.turn.append_reply(_calling("c1"))
        raise RuntimeError("the invocation is gone")

    ask = asking(run)

    async def failure(text: str, invocation: Any = None) -> tuple[str, int]:
        with pytest.raises(restate.TerminalError) as error:
            await ask(text, invocation)
        return error.value.message, error.value.status_code

    boom, stop, gone = request(), request(), request()
    # Retried (after a crash right after the close, say): each fails as it did, not with the note as its answer.
    assert [await failure("boom", boom), await failure("boom", boom)] == [("boom", 500)] * 2
    assert [await failure("stop", stop), await failure("stop", stop)] == [("cancelled", 409)] * 2
    with pytest.raises(RuntimeError, match="gone"):
        await ask("gone", gone)
    # The next message closes that turn, ended; the gone invocation, run again, fails with the note.
    assert await failure("boom") == ("boom", 500)
    assert await failure("gone", gone) == (ENDED, 500)


class _OtherFramework(Items):
    """Another framework's items, which have a ``kind``: it cannot read the made-up framework's."""

    def load(self, stored: str) -> Item:
        item = json.loads(stored)
        if "kind" not in item:
            raise ValueError("an item has a kind")
        return item


async def test_a_conversation_this_agent_cannot_read_fails_for_good():
    """An agent of the same name on another framework fails with a terminal error: a retry would read the same row."""
    obj = sessions.SessionObject("asker")

    async def run(conversation: Conversation) -> str:
        await conversation.turn.append_reply({"role": "assistant", "text": "hello"})
        return "hello"

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, ITEMS, run)

    @obj.handler
    async def ask_other(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, _OtherFramework(), run)

    backend = Backend()
    assert await invoke(obj, "ask", Host(backend, service="asker", answers=PUBLISHED), request(), "hi") == "hello"
    with pytest.raises(restate.TerminalError, match="cannot read .* ValueError: an item has a kind"):
        await invoke(obj, "ask_other", Host(backend, service="asker", answers=PUBLISHED), request(), "hi")


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


async def test_a_turn_left_without_an_answer_is_closed_once_however_a_crash_cuts_the_close():
    from restate.agents.internal import ENDED  # pylint: disable=import-outside-toplevel

    async def run(conversation: Conversation) -> str:
        if conversation.turn.prompt.item["text"] == "first":
            await conversation.turn.append_reply(_calling("c1"))
            raise RuntimeError("the invocation is gone")
        return "fine"

    obj = sessions.SessionObject("asker")

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, ITEMS, run)

    @obj.handler
    async def first(ctx: sessions.SessionContext) -> list[Any]:
        conversation = await Conversation.load(ctx.db, ITEMS, "all")
        return [entry.item for entry in conversation.entries if entry.turn == conversation.transcript[0].n]

    def host(backend: Backend) -> Host:
        return Host(backend, service="asker", answers=PUBLISHED)

    crashes = 0
    for syncs in range(100):
        backend = Backend()
        with pytest.raises(RuntimeError, match="gone"):
            await invoke(obj, "ask", host(backend), request(), "first")
        # The second prompt closes the first's turn, ended; its process crashes after ``syncs`` syncs.
        second, crashing = request(), host(backend)
        crashing.crash_after(syncs)
        try:
            assert await invoke(obj, "ask", crashing, second, "second") == "fine"
            break
        except RuntimeError as error:
            assert str(error) == "the sync failed"
        crashes += 1
        assert await invoke(obj, "ask", host(backend), second, "second") == "fine"
        items = await invoke(obj, "first", host(backend), request())
        notes = [item for item in items if item == {"role": "assistant", "text": ENDED}]
        results = [item for item in items if "result" in item]
        assert (syncs, len(notes), results) == (syncs, 1, [{"result": "c1", "output": ENDED}])
    assert crashes > 0


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

    def check(answer: str, last: bool) -> str | None:
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


async def test_a_turn_whose_reply_is_still_not_what_was_asked_for_after_asking_again_fails_with_its_checks_error():
    replies = iter(["prose", "still prose"])
    seen: list[Conversation] = []

    async def run(conversation: Conversation) -> str:
        seen.append(conversation)
        reply = next(replies)
        await conversation.turn.append_reply({"role": "assistant", "text": reply})
        return reply

    def check(answer: str, last: bool) -> str | None:
        if answer.startswith("{"):
            return None
        if last:
            raise restate.TerminalError("not JSON, asked again")
        return "Reply again, with JSON only."

    ask = asking_with(run, check)
    invocation = request()
    with pytest.raises(restate.TerminalError, match="^not JSON, asked again$"):
        await ask("plan", invocation)
    # The turn ends failed, with the error: not answered with a reply the caller never got.
    turn = seen[-1].turn
    assert (turn.status, turn.answer) == ("failed", "Error: not JSON, asked again")
    assert [e.item for e in turn.items][-2:] == [
        {"role": "assistant", "text": "still prose"},
        {"role": "assistant", "text": "Error: not JSON, asked again"},
    ]
    # Retried (a crash right after the close, say): it fails as it did.
    with pytest.raises(restate.TerminalError, match="^not JSON, asked again$"):
        await ask("plan", invocation)


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


async def test_the_last_position_and_seq_are_read_from_the_ends_of_the_tables_without_a_scan():
    """``last_of`` runs at every turn and in the views: a scan would read the whole database in a new process."""
    obj = sessions.SessionObject("filler")
    text = random.Random(7)

    @obj.handler
    async def fill(ctx: sessions.SessionContext, first: int) -> None:
        # A hundred turns of three long results each, laid out in the file as turns are: each among its steps.
        await SCHEMA.ensure(ctx.db)
        async with ctx.db.transaction() as tx:
            for turn in range(first, first + 400, 4):
                await tx.execute(
                    f"INSERT INTO {TURNS} (n, seq, prompt, item, status, answer, answered_at, answered_seq) "
                    "VALUES (?, ?, ?, ?, 'answered', 'done', ?, ?)",
                    [turn, turn, f"inv_{turn}:0", json.dumps(ITEMS.prompt("go")), turn + 3, turn + 3],
                )
                for step in range(1, 4):
                    output = "".join(text.choices("abcdefghijklmnopqrstuvwxyz ", k=2000))
                    await tx.execute(
                        f"INSERT INTO {STEPS} (n, turn, seq, item) VALUES (?, ?, ?, ?)",
                        [turn + step, turn, turn + step, json.dumps({"result": f"c{step}", "output": output})],
                    )

    @obj.view
    async def ends(ctx: sessions.ViewContext) -> list[int | None]:
        return list(await last_of(ctx.db))

    @obj.view
    async def scan(ctx: sessions.ViewContext) -> None:
        await ctx.db.values(f"SELECT (SELECT SUM(LENGTH(item)) FROM {TURNS}), (SELECT SUM(LENGTH(item)) FROM {STEPS})")

    backend = Backend()
    for first in range(0, 2400, 400):
        await invoke(obj, "fill", Host(backend, service="filler"), request(), first)

    class Counting(Host):
        packs: int = 0

        def get(self, key: str):
            self.packs += "/pack/" in key
            return super().get(key)

    async def cold(handler: str, key: str) -> tuple[Any, int]:
        # The stored state under a key of its own: no process has read it, so every page it needs is a get.
        copy = Backend()
        copy.stored = dict(backend.stored)
        host = Counting(copy, service="filler", key=key, shared=True)
        return await invoke(obj, handler, host, request()), host.packs

    _, scanned = await cold("scan", "scanned")
    last, read = await cold("ends", "ended")
    assert last == [2399, 2399]
    # A scan reads every part of the file; the maxima, at most a part for the end of each of the five b-trees
    # they come from (both tables, and the seq and answered_seq indexes). A SELECT of several maxima scanned.
    assert scanned >= 20
    assert read <= 5


# --- A step that no tool waits for commits with the next write --------------------------------------


class Syncing(Host):
    """A host that counts its syncs (each a commit of the database), and crashes at a publish once armed."""

    syncs: int = 0
    crash_at_publish: bool = False

    async def sync(self) -> None:
        await super().sync()
        self.syncs += 1

    def generic_call(self, service, handler, arg, key=None, idempotency_key=None, headers=None):
        if handler == "publish" and self.crash_at_publish:
            # The process crashes once the clients were told to read: every later sync fails.
            self.crash_after(0)
        return super().generic_call(service, handler, arg, key, idempotency_key, headers)


def _turn_rows() -> sessions.SessionObject:
    """An object whose ``ask`` answers with the run that ``runs`` holds, and whose ``rows`` reads each turn back.

    It has a companion, as an agent has, which ``publish`` publishes to: its turns, outside an agent's ``chat``,
    publish nothing themselves."""
    obj = sessions.SessionObject("asker")
    with_companion(obj)

    @obj.handler
    async def rows(ctx: sessions.SessionContext) -> list[dict[str, Any]]:
        conversation = await Conversation.load(ctx.db, ITEMS, "all")
        return [
            {"answer": turn.answer, "items": [[entry.n, entry.item] for entry in turn.items]}
            for turn in conversation.turns
        ]

    return obj


def _syncing(backend: Backend) -> Syncing:
    return Syncing(backend, service="asker", answers=PUBLISHED)


async def test_a_turn_that_ends_with_a_reply_commits_the_reply_and_its_answer_at_once():
    host: list[Syncing] = []
    before: list[int] = []

    async def run(conversation: Conversation) -> str:
        before.append(host[0].syncs)
        await conversation.turn.append_reply({"role": "assistant", "text": "hello"})
        return "hello"

    obj = _turn_rows()

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, ITEMS, run)

    backend = Backend()
    for text in ("first", "second"):
        host[:] = [_syncing(backend)]
        assert await invoke(obj, "ask", host[0], request(), text) == "hello"
    # The second turn (the conversation's tables exist): from the reply on, one commit, with the answer.
    assert host[0].syncs - before[-1] == 1
    turns = await invoke(obj, "rows", _syncing(backend), request())
    assert [turn["answer"] for turn in turns] == ["hello", "hello"]


async def test_a_crash_at_the_final_commit_stores_neither_the_reply_nor_the_answer_and_the_retry_asks_again():
    runs: list[str] = []

    async def run(conversation: Conversation) -> str:
        runs.append("ran")
        await conversation.turn.append_reply({"role": "assistant", "text": f"hello {len(runs)}"})
        return f"hello {len(runs)}"

    obj = _turn_rows()

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, ITEMS, run)

    crashed = 0
    for syncs in range(20):
        backend, invocation = Backend(), request()
        runs.clear()
        crashing = _syncing(backend)
        crashing.crash_after(syncs)
        try:
            await invoke(obj, "ask", crashing, invocation, "hi")
            break
        except RuntimeError as error:
            assert str(error) == "the sync failed"
        stored = await invoke(obj, "rows", _syncing(backend), request())
        # Never a reply without its answer: the two commit together, or not at all.
        answered = [{"answer": "hello 1", "items": [[1, {"role": "assistant", "text": "hello 1"}]]}]
        assert stored in ([], [{"answer": None, "items": []}], answered), stored
        # A crash after the model answered, at the final commit: neither was stored.
        crashed += len(runs) == 1 and stored != answered
        answer = await invoke(obj, "ask", _syncing(backend), invocation, "hi")
        # The retried attempt asked the model again, or returned the stored answer; one reply, one answer.
        assert answer == f"hello {len(runs)}"
        assert len(runs) == (2 if stored == [{"answer": None, "items": []}] else 1)
        turns = await invoke(obj, "rows", _syncing(backend), request())
        assert turns == [{"answer": answer, "items": [[1, {"role": "assistant", "text": answer}]]}]
    assert crashed == 1


async def test_a_reply_held_back_is_stored_before_a_publish_that_follows_it():
    from restate.agents._turn import publish  # pylint: disable=import-outside-toplevel

    obj = _turn_rows()

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        async def run(conversation: Conversation) -> str:
            await conversation.turn.append_reply({"role": "assistant", "text": "on it"})
            # The clients are told to read the reply: it is stored by then, even if the process crashes after.
            # ``publish`` takes an agent's context; this object stands in for one, so the test says so.
            await publish(typing.cast(Any, ctx))
            await conversation.turn.append_reply({"role": "assistant", "text": "done"})
            return "done"

        return await answer_once(ctx, text, ITEMS, run)

    backend = Backend()
    host = _syncing(backend)
    host.crash_at_publish = True
    with pytest.raises(RuntimeError, match="the sync failed"):
        await invoke(obj, "ask", host, request(), "hi")
    turns = await invoke(obj, "rows", _syncing(backend), request())
    assert turns == [{"answer": None, "items": [[1, {"role": "assistant", "text": "on it"}]]}]


async def test_a_run_that_fails_its_attempt_after_a_reply_leaves_the_reply_stored():
    async def run(conversation: Conversation) -> str:
        await conversation.turn.append_reply({"role": "assistant", "text": "half way"})
        raise RuntimeError("the run failed")

    obj = _turn_rows()

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, ITEMS, run)

    backend = Backend()
    with pytest.raises(RuntimeError, match="the run failed"):
        await invoke(obj, "ask", _syncing(backend), request(), "hi")
    # The retry carries on from it, as from any step stored before the attempt failed.
    turns = await invoke(obj, "rows", _syncing(backend), request())
    assert turns == [{"answer": None, "items": [[1, {"role": "assistant", "text": "half way"}]]}]


async def test_appends_at_once_with_steps_held_back_get_positions_of_their_own_in_order():
    seen: list[list[tuple[int, Item]]] = []

    async def run(conversation: Conversation) -> str:
        turn = conversation.turn
        await turn.append_reply(_calling("c1", "c2"))
        rng = random.Random(7)

        async def appended(item: Item) -> None:
            for _ in range(rng.randrange(3)):
                await asyncio.sleep(0)
            await turn.append(item)

        notes = [{"role": "assistant", "text": f"note {i}"} for i in range(4)]
        results = [{"result": call_id, "output": "done"} for call_id in ("c1", "c2")]
        await asyncio.gather(*(appended(item) for item in [notes[0], results[0], notes[1], notes[2], results[1]]))
        await turn.append_reply(notes[3])
        seen.append([(entry.n, entry.item) for entry in turn.items])
        return "note 3"

    obj = _turn_rows()

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, ITEMS, run)

    @obj.handler
    async def seqs(ctx: sessions.SessionContext) -> list[Any]:
        return [row[0] for row in await ctx.db.values(f"SELECT seq FROM {STEPS} ORDER BY n")]

    backend = Backend()
    assert await invoke(obj, "ask", _syncing(backend), request(), "hi") == "note 3"
    [turn] = await invoke(obj, "rows", _syncing(backend), request())
    stored = [(n, item) for n, item in turn["items"]]
    positions = [n for n, _ in stored]
    # Each item has a position of its own, the next one, and is stored where the run saw it, its seq in order.
    assert positions == list(range(1, 8))
    assert [tuple(entry) for entry in stored] == seen[0]
    assert await invoke(obj, "seqs", _syncing(backend), request()) == list(range(2, 9))


@pytest.mark.parametrize("write", ["append", "answered", "snapshot", "drop"])
async def test_a_write_cancelled_while_it_commits_keeps_its_bookkeeping_and_the_turn_closes_after_it(write: str):
    """A stop cancels the run (``Stop.guard``), here while a write waits for its commit: the write lands with the
    ledger's positions, ``seq`` and held rows, and the turn's close stores after it rather than over it."""
    gate: dict[str, Any] = {}

    class Gated(Host):
        async def sync(self) -> None:
            if gate.pop("armed", False):
                gate["blocked"].set()
                await gate["release"].wait()
            await super().sync()

    async def run(conversation: Conversation) -> str:
        turn = conversation.turn
        await turn.append_reply(_calling("c1"))
        await turn.append({"result": "c1", "output": "done"})
        # Held: written with the next write.
        await turn.append_reply({"role": "assistant", "text": "noted"})
        writes = {
            "append": lambda: turn.append_reply(_calling("c2")),
            "answered": lambda: turn.answered("noted"),
            "snapshot": lambda: turn.snapshot([{"role": "user", "text": "summary"}]),
            "drop": lambda: turn.drop(typing.cast(int, turn.last_reply())),
        }
        gate.update(armed=True, blocked=asyncio.Event(), release=asyncio.Event())
        task = asyncio.ensure_future(writes[write]())
        await gate["blocked"].wait()
        task.cancel()
        gate["release"].set()
        with pytest.raises(asyncio.CancelledError):
            await task
        raise restate.TerminalError("cancelled", status_code=409)

    obj = _turn_rows()

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, ITEMS, run)

    backend = Backend()
    with pytest.raises(restate.TerminalError, match="cancelled"):
        await invoke(obj, "ask", Gated(backend, service="asker", answers=PUBLISHED), request(), "hi")
    [turn] = await invoke(obj, "rows", _syncing(backend), request())
    texts = [item.get("text", item.get("output")) for _, item in turn["items"]]
    expected = {
        "append": (CANCELLED, ["", "done", "noted", "", CANCELLED, CANCELLED]),
        "answered": ("noted", ["", "done", "noted"]),
        "snapshot": (CANCELLED, ["", "done", "noted", CANCELLED]),
        "drop": (CANCELLED, ["", "done", CANCELLED]),
    }
    assert (turn["answer"], texts) == expected[write]


async def test_a_keys_first_turn_applies_its_schemas_in_one_commit():
    """The conversation's tables and the tool calls' (``_calls.py``), which the turn reads as it ends, are made in
    one commit, though no tool runs: a key's first turn pays one commit for its schemas, not two."""

    async def run(conversation: Conversation) -> str:
        await conversation.turn.append_reply({"role": "assistant", "text": "hello"})
        return "hello"

    obj = _turn_rows()

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        return await answer_once(ctx, text, ITEMS, run)

    backend = Backend()
    syncs: list[int] = []
    for text in ("first", "second"):
        host = _syncing(backend)
        assert await invoke(obj, "ask", host, request(), text) == "hello"
        syncs.append(host.syncs)
    assert syncs[0] - syncs[1] == 1


# --- Layers: the conversation stores, an agent's turn publishes -------------------------------------------------


def _no_companion(*_: Any) -> Any:
    raise AssertionError("a session object that is no agent has no companion to call, nor anything to publish")


async def test_answer_once_in_a_plain_session_object_stores_a_turn_of_tool_calls_and_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
):
    """The conversation only stores: its commits publish nothing unless an agent's ``chat`` turn asks for it."""
    from restate.agents import _signals, _turn  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(_turn, "publish", _no_companion)
    monkeypatch.setattr(_signals, "companion_at", _no_companion)
    obj = sessions.SessionObject("desk")

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        async def run(conversation: Conversation) -> str:
            calls = [{"id": "t1", "name": "lookup", "input": {}}]
            await conversation.turn.append_reply({"role": "assistant", "text": "", "calls": calls})
            await conversation.turn.append({"result": "t1", "output": "found"})
            await conversation.turn.append_reply({"role": "assistant", "text": "done"})
            return "done"

        return await answer_once(ctx, text, ITEMS, run)

    @obj.handler
    async def rows(ctx: sessions.SessionContext) -> list[Any]:
        conversation = await Conversation.load(ctx.db, ITEMS, "all")
        return [[turn.answer, len(turn.items)] for turn in conversation.turns]

    backend = Backend()
    host = Host(backend, service="desk")
    assert await invoke(obj, "ask", host, request(), "hi") == "done"
    assert host.calls == []
    assert await invoke(obj, "rows", Host(backend, service="desk"), request()) == [["done", 3]]


async def test_a_publish_from_a_session_object_that_is_no_agent_raises():
    from restate.agents.internal import publish  # pylint: disable=import-outside-toplevel

    obj = sessions.SessionObject("desk")

    @obj.handler
    async def tell(ctx: sessions.SessionContext) -> None:
        # The cast is the test: ``publish`` takes an agent's context, and a plain one passed off as one still
        # raises when it runs. The names are a signpost; the companion is the fence.
        await publish(typing.cast(Any, ctx))

    host = Host(Backend(), service="desk")
    with pytest.raises(TypeError, match="companion_in takes the context of an agent's handler"):
        await invoke(obj, "tell", host, request())
    assert host.calls == []
