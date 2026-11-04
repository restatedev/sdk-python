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
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest
from sessions_database import Backend
from sessions_object import Host, request

import restate
from restate import sessions
from restate.agents._conversation import Call, Conversation, Item, Result, answer_once, table_of
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

    def prompt(self, text: str) -> Item:
        return {"role": "user", "text": text}

    def reply(self, text: str) -> Item:
        return {"role": "assistant", "text": text}

    def results(self, results: Sequence[tuple[Call, str]]) -> list[Item]:
        return [{"result": call.id, "output": text} for call, text in results]

    def calls(self, item: Item) -> list[Call]:
        return [Call(c["id"], c["name"], c["input"]) for c in item.get("calls", [])]

    def results_of(self, item: Item) -> list[Result]:
        return [Result(item["result"], item["output"])] if "result" in item else []

    def said(self, item: Item) -> tuple[str, str] | None:
        return (item["role"], item["text"]) if "role" in item else None


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


async def test_the_transcript_follows_appends_and_drops():
    async def run(conversation: Conversation) -> str:
        transcript = conversation.transcript()  # built now, then kept up to date
        call = {"id": "c1", "name": "lookup", "input": {"q": 1}}
        await conversation.append_reply({"role": "assistant", "text": "looking", "calls": [call]})
        await conversation.append({"result": "c1", "output": "found"})
        assert transcript.messages()[-1]["parts"][-1] == {
            "type": "tool",
            "id": "c1",
            "name": "lookup",
            "input": {"q": 1},
            "output": "found",
        }
        blocked = await conversation.append_reply({"role": "assistant", "text": "blocked"})
        assert blocked == conversation.entries[-1].n  # a reply's id: the position of its first item
        await conversation.drop(blocked)
        texts = [part.get("text") for message in conversation.transcript().messages() for part in message["parts"]]
        assert "blocked" not in texts
        return "done"

    assert await asking(run)("hi") == "done"


class OtherItems(Items):
    """Another made-up framework, of the same items."""


def reply_with(text: str, seen: list[list[str]] | None = None) -> Run:
    """Return a run that replies ``text``, after keeping the prompts it saw (in ``seen``)."""

    async def run(conversation: Conversation) -> str:
        if seen is not None:
            seen.append([e.item["text"] for e in conversation.entries if e.prompt is not None])
        await asyncio.sleep(0.01)
        await conversation.append_reply({"role": "assistant", "text": text})
        return text

    return run


def handler(body: Callable[[sessions.SessionContext], Awaitable[Any]]) -> Callable[[], Awaitable[Any]]:
    """Return how to invoke a handler that runs ``body``, in a backend of its own."""
    obj = sessions.SessionObject("asker")

    @obj.handler
    async def run(ctx: sessions.SessionContext, text: str) -> Any:
        return await body(ctx)

    async def invoked() -> Any:
        return await invoke(obj, "run", Host(Backend(), service="asker", answers=PUBLISHED), request(), "")

    return invoked


async def test_two_prompts_at_once_need_conversations_of_their_own():
    async def same(ctx: sessions.SessionContext) -> Any:
        one = answer_once(ctx, "a", ITEMS, reply_with("one"))
        return await asyncio.gather(one, answer_once(ctx, "b", ITEMS, reply_with("two")))

    with pytest.raises(restate.TerminalError, match="conversation of its own"):
        await handler(same)()

    async def apart(ctx: sessions.SessionContext) -> Any:
        one = answer_once(ctx, "a", ITEMS, reply_with("one"))
        return await asyncio.gather(one, answer_once(ctx, "b", ITEMS, reply_with("two"), name="summaries"))

    assert await handler(apart)() == ["one", "two"]


async def test_two_agents_of_a_key_keep_conversations_of_their_own_by_name():
    seen: list[list[str]] = []

    async def both(ctx: sessions.SessionContext) -> Any:
        first = await answer_once(ctx, "to the planner", ITEMS, reply_with("p", seen), name="planner")
        second = await answer_once(ctx, "to the writer", OtherItems(), reply_with("w", seen), name="writer")
        third = await answer_once(ctx, "to the planner again", ITEMS, reply_with("p", seen), name="planner")
        return [first, second, third]

    assert await handler(both)() == ["p", "w", "p"]
    assert seen == [["to the planner"], ["to the writer"], ["to the planner", "to the planner again"]]


def test_a_conversations_name_is_an_identifier():
    with pytest.raises(ValueError, match="lowercase identifier"):
        table_of("x; DROP TABLE y")
    assert table_of() == "restate_agent_conversation"
    assert table_of("planner") == "restate_agent_conversation__planner"
