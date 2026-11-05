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
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest
from sessions_database import Backend
from sessions_object import Host, request

import restate
from restate import sessions
from restate.agents._conversation import Call, Conversation, Item, Parsed, Result, answer_once
from restate.agents.internal import CANCELLED
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
    with pytest.raises(RuntimeError, match="stores nothing more"):
        await conversation.append({"role": "assistant", "text": "too late"})


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
