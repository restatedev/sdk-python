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
from restate.agents._conversation import Call, Conversation, Item, Parsed, Result, answer_once
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

    def reply(self, text: str) -> Item:
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
    from restate.agents._conversation import Entry  # pylint: disable=import-outside-toplevel

    return Conversation(typing.cast(Any, _Recorded()), ITEMS, [Entry(n, item) for n, item in enumerate(items)])


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
