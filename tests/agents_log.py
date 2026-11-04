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
"""An agent's conversation log: what a transcript stores, and what clients read of it."""

# pylint: disable=missing-function-docstring

import typing

import pytest

from restate import sessions
from restate.agents import _log
from restate.agents._api import Messages, MessagesRequest
from restate.agents._transcript import Transcript
from sessions_database import Backend  # pylint: disable=import-error
from sessions_object import Host, run  # pylint: disable=import-error


@pytest.fixture
def anyio_backend():
    return "asyncio"


chat = sessions.SessionObject("logged")
transcripts: dict[str, Transcript] = {}


@chat.handler
async def store(ctx: sessions.SessionContext, name: str) -> None:
    await _log.sync(ctx, transcripts[name].messages())


@chat.handler
async def start_again(ctx: sessions.SessionContext, start: list[int]) -> None:
    async with ctx.db.transaction() as tx:
        await tx.execute("DELETE FROM restate_agent_log")
        await _log.restart(tx, _log.Start(seq=start[0], id=start[1]))


@chat.view
async def read(ctx: sessions.ViewContext, request: MessagesRequest) -> Messages:
    return await _log.page(ctx.db, request)


def said(name: str) -> Transcript:
    transcripts[name] = Transcript()
    return transcripts[name]


def texts(page: Messages) -> list[tuple[int, str]]:
    return [
        (message["id"], " ".join(typing.cast(str, part.get("text", part.get("name"))) for part in message["parts"]))
        for message in page["messages"]
    ]


async def test_a_transcript_is_stored_and_read_in_pages_and_from_a_cursor():
    host = Host(Backend(), service="logged")
    transcript = said("first")
    transcript.say("user", "What is the weather in Lisbon?")
    transcript.call("call-1", "get_weather", {"city": "Lisbon"})
    await run(chat, "store", host, "first")
    page = await run(chat, "read", host.view(), {})
    assert texts(page) == [(0, "What is the weather in Lisbon?"), (1, "get_weather")]
    cursor = page["cursor"]

    # The tool call's output comes: its message grows in place, keeps its id, and moves past the cursor.
    transcript.result("call-1", "Sunny")
    transcript.say("assistant", "It is sunny in Lisbon.")
    await run(chat, "store", host, "first")
    changed = await run(chat, "read", host.view(), {"cursor": cursor})
    assert [message["id"] for message in changed["messages"]] == [1, 2]
    tool = changed["messages"][0]["parts"][0]
    assert tool == {
        "type": "tool",
        "id": "call-1",
        "name": "get_weather",
        "input": {"city": "Lisbon"},
        "output": "Sunny",
    }

    # Nothing changed: nothing is written, and the cursor stays.
    await run(chat, "store", host, "first")
    assert (await run(chat, "read", host.view(), {}))["cursor"] == changed["cursor"]
    assert (await run(chat, "read", host.view(), {"cursor": changed["cursor"]}))["messages"] == []


async def test_an_error_output_is_marked_and_answered_only_by_its_own_error():
    transcript = Transcript()
    transcript.call("call-1", "charge", {"amount": 5})
    assert not transcript.answered("call-1")
    transcript.result("call-1", "Error: card declined")
    assert transcript.messages()[0]["parts"][0].get("error") is True
    # An error is the call's result only if the call answered with it (see the calls' bookkeeping).
    assert not transcript.answered("call-1")
    assert transcript.answered("call-1", failed="Error: card declined")


async def test_a_replaced_transcript_leaves_its_messages_in_the_log():
    host = Host(Backend(), service="logged")
    transcript = said("long")
    for n in range(3):
        transcript.say("user", f"message {n}")
    await run(chat, "store", host, "long")
    compacted = said("long")
    compacted.say("assistant", "A summary of it all")
    await run(chat, "store", host, "long")
    page = await run(chat, "read", host.view(), {})
    assert texts(page) == [(0, "message 0"), (1, "message 1"), (2, "message 2"), (3, "A summary of it all")]
    assert texts(await run(chat, "read", host.view(), {"limit": 2})) == [(2, "message 2"), (3, "A summary of it all")]
    assert texts(await run(chat, "read", host.view(), {"before": 2})) == [(0, "message 0"), (1, "message 1")]


async def test_a_restarted_log_goes_on_from_where_the_cleared_one_got():
    host = Host(Backend(), service="logged")
    transcript = said("cleared")
    transcript.say("user", "before")
    await run(chat, "store", host, "cleared")
    await run(chat, "start_again", host, [10, 5])
    after = said("cleared")
    after.say("user", "after")
    await run(chat, "store", host, "cleared")
    page = await run(chat, "read", host.view(), {})
    assert texts(page) == [(5, "after")]
    assert page["first"] == 5 and page["cursor"] == 11
