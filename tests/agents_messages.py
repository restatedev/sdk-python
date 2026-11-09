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
"""An agent's conversation as clients read it: pages and cursors, rendered from the conversation's own rows."""

# pylint: disable=missing-function-docstring

import typing
from typing import Any

import pytest
from agents_conversation import ITEMS  # pylint: disable=import-error
from sessions_database import Backend  # pylint: disable=import-error
from sessions_object import Host, run  # pylint: disable=import-error

from restate import sessions
from restate.agents import _messages
from restate.agents._api import Messages, MessagesRequest
from restate.agents._conversation import STEPS, TURNS, Conversation, Entry, next_start, restart


@pytest.fixture
def anyio_backend():
    return "asyncio"


chat = sessions.SessionObject("rendered")


@chat.handler
async def ask(ctx: sessions.SessionContext, text: str) -> None:
    conversation = await Conversation.load(ctx.db, ITEMS)
    # Turns never overlap: the one before is answered first, with its last reply.
    if (running := conversation.running()) is not None:
        await running.answered(running.final_reply() or "")
    await conversation.ask(f"{ctx.request.id}:{len(conversation.transcript)}", text)


@chat.handler
async def answer(ctx: sessions.SessionContext, text: str) -> None:
    conversation = await Conversation.load(ctx.db, ITEMS)
    await conversation.turn.answered(text)


@chat.handler
async def reply(ctx: sessions.SessionContext, items: list[Any]) -> int:
    conversation = await Conversation.load(ctx.db, ITEMS)
    return await conversation.turn.append_reply(*items)


@chat.handler
async def append(ctx: sessions.SessionContext, items: list[Any]) -> None:
    conversation = await Conversation.load(ctx.db, ITEMS)
    await conversation.turn.append(*items)


@chat.handler
async def drop(ctx: sessions.SessionContext, reply_id: int) -> None:
    conversation = await Conversation.load(ctx.db, ITEMS)
    await conversation.turn.drop(reply_id)


@chat.handler
async def clear(ctx: sessions.SessionContext) -> None:
    await Conversation.load(ctx.db, ITEMS)
    start = await next_start(ctx.db)
    async with ctx.db.transaction() as tx:
        await tx.execute(f"DELETE FROM {TURNS}")
        await tx.execute(f"DELETE FROM {STEPS}")
        await restart(tx, start)


@chat.view
async def read(ctx: sessions.ViewContext, request: MessagesRequest) -> Messages:
    return await _messages.page(ctx.db, ITEMS, request)


def texts(page: Messages) -> list[tuple[int, str]]:
    return [
        (message["id"], " ".join(typing.cast(str, part.get("text", part.get("name"))) for part in message["parts"]))
        for message in page["messages"]
    ]


WEATHER = {"id": "call-1", "name": "get_weather", "input": {"city": "Lisbon"}}


async def test_the_conversation_is_read_in_pages_and_from_a_cursor():
    host = Host(Backend(), service="rendered")
    await run(chat, "ask", host, "What is the weather in Lisbon?")
    await run(chat, "reply", host, [{"role": "assistant", "text": "", "calls": [WEATHER]}])
    page = await run(chat, "read", host.view(), {})
    assert texts(page) == [(0, "What is the weather in Lisbon?"), (1, "get_weather")]
    cursor = page["cursor"]

    # The tool call's output comes: its message grows in place, keeps its id, and moves past the cursor.
    await run(chat, "append", host, [{"result": "call-1", "output": "Sunny"}])
    await run(chat, "reply", host, [{"role": "assistant", "text": "It is sunny in Lisbon."}])
    changed = await run(chat, "read", host.view(), {"cursor": cursor})
    assert [message["id"] for message in changed["messages"]] == [1, 3]
    assert changed["messages"][0]["parts"] == [{**WEATHER, "type": "tool", "output": "Sunny"}]

    # Nothing changed since: nothing to read, and the cursor stays.
    assert (await run(chat, "read", host.view(), {}))["cursor"] == changed["cursor"]
    assert (await run(chat, "read", host.view(), {"cursor": changed["cursor"]}))["messages"] == []


async def test_a_page_reads_back_only_as_many_prompts_as_it_shows():
    host = Host(Backend(), service="rendered")
    for n in range(5):
        await run(chat, "ask", host, f"question {n}")
        await run(chat, "reply", host, [{"role": "assistant", "text": f"answer {n}"}])
    latest = await run(chat, "read", host.view(), {"limit": 3})
    assert texts(latest) == [(7, "answer 3"), (8, "question 4"), (9, "answer 4")] and latest["more"]
    earlier = await run(chat, "read", host.view(), {"limit": 3, "before": 7})
    assert texts(earlier) == [(4, "question 2"), (5, "answer 2"), (6, "question 3")] and earlier["more"]
    first = await run(chat, "read", host.view(), {"limit": 3, "before": 2})
    assert texts(first) == [(0, "question 0"), (1, "answer 0")] and not first["more"]


async def test_a_page_of_no_messages_has_the_cursor_and_whether_there_are_any():
    host = Host(Backend(), service="rendered")
    for n in range(2):
        await run(chat, "ask", host, f"question {n}")
        await run(chat, "reply", host, [{"role": "assistant", "text": f"answer {n}"}])
    every = await run(chat, "read", host.view(), {})
    for view in ("full", "summary"):
        none = await run(chat, "read", host.view(), {"limit": 0, "view": view})
        assert (view, none["messages"], none["more"], none["cursor"]) == (view, [], True, every["cursor"])


async def test_an_error_output_is_marked_and_is_a_calls_result_only_if_it_is_its_own_error():
    host = Host(Backend(), service="rendered")
    await run(chat, "ask", host, "Charge me")
    charge = {"id": "call-1", "name": "charge", "input": {"amount": 5}}
    await run(chat, "reply", host, [{"role": "assistant", "text": "", "calls": [charge]}])
    await run(chat, "append", host, [{"result": "call-1", "output": "Error: card declined"}])
    [_, message] = (await run(chat, "read", host.view(), {}))["messages"]
    assert message["parts"][0].get("error") is True

    entries = [
        Entry(0, {"role": "user", "text": "go"}, prompt="p0"),
        Entry(1, {"result": "call-1", "output": "Error: no"}),
    ]
    turn = Conversation(typing.cast(Any, None), ITEMS, entries).turn
    # An error is the call's result only if the call answered with it (see the calls' bookkeeping).
    assert not turn.stored("call-1")
    assert turn.stored("call-1", failed="Error: no")


async def test_a_dropped_reply_is_replaced_under_its_ids_and_a_cursor_sees_the_replacement():
    host = Host(Backend(), service="rendered")
    await run(chat, "ask", host, "Hi")
    blocked = await run(chat, "reply", host, [{"role": "assistant", "text": "blocked"}])
    cursor = (await run(chat, "read", host.view(), {}))["cursor"]
    await run(chat, "drop", host, blocked)
    await run(chat, "reply", host, [{"role": "assistant", "text": "fine"}])
    changed = await run(chat, "read", host.view(), {"cursor": cursor})
    assert texts(changed) == [(blocked, "fine")]
    assert texts(await run(chat, "read", host.view(), {})) == [(0, "Hi"), (1, "fine")]


async def test_a_cleared_conversation_goes_on_from_its_ids_and_cursors():
    host = Host(Backend(), service="rendered")
    await run(chat, "ask", host, "before")
    await run(chat, "reply", host, [{"role": "assistant", "text": "noted"}])
    cursor = (await run(chat, "read", host.view(), {}))["cursor"]
    await run(chat, "clear", host)
    emptied = await run(chat, "read", host.view(), {})
    assert emptied["messages"] == [] and emptied["first"] == 2 and emptied["cursor"] == cursor
    await run(chat, "ask", host, "after")
    page = await run(chat, "read", host.view(), {})
    assert texts(page) == [(2, "after")] and page["first"] == 2 and page["cursor"] == cursor + 1
    assert texts(await run(chat, "read", host.view(), {"cursor": cursor})) == [(2, "after")]


async def test_the_summary_is_each_turns_message_and_answer_under_their_ids():
    host = Host(Backend(), service="rendered")
    await run(chat, "ask", host, "What is the weather in Lisbon?")
    await run(chat, "reply", host, [{"role": "assistant", "text": "", "calls": [WEATHER]}])
    await run(chat, "append", host, [{"result": "call-1", "output": "Sunny"}])
    await run(chat, "reply", host, [{"role": "assistant", "text": "It is sunny in Lisbon."}])
    # The turn has no answer yet: the summary is its message alone, while the full view shows its steps.
    before = await run(chat, "read", host.view(), {"view": "summary"})
    assert texts(before) == [(0, "What is the weather in Lisbon?")]
    await run(chat, "answer", host, "Sunny, 24 degrees.")
    await run(chat, "ask", host, "Thanks")
    summary = await run(chat, "read", host.view(), {"view": "summary"})
    # The answer has the id of the turn's last message of the assistant, as the full view shows it.
    assert texts(summary) == [(0, "What is the weather in Lisbon?"), (3, "Sunny, 24 degrees."), (4, "Thanks")]
    full = await run(chat, "read", host.view(), {})
    assert [m["id"] for m in full["messages"]] == [0, 1, 3, 4]
    # A follower of the summary gets the answer once it is stored, then the next message, in that order.
    changed = await run(chat, "read", host.view(), {"view": "summary", "cursor": before["cursor"]})
    assert texts(changed) == [(3, "Sunny, 24 degrees."), (4, "Thanks")]
    assert (await run(chat, "read", host.view(), {"view": "summary", "cursor": changed["cursor"]}))["messages"] == []
    # Paging back by turns, as the full view pages.
    page = await run(chat, "read", host.view(), {"view": "summary", "limit": 2})
    assert texts(page) == [(3, "Sunny, 24 degrees."), (4, "Thanks")] and page["more"]
    earlier = await run(chat, "read", host.view(), {"view": "summary", "limit": 2, "before": 3})
    assert texts(earlier) == [(0, "What is the weather in Lisbon?")] and not earlier["more"]
