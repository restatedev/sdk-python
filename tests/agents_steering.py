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
"""Steering, in process: a message steered to a turn is stored once, as a step of the turn, and the model keeps
seeing it. The companion is tested against a server in ``agents_signals.py``, and the agent's ``steered`` in
``agents_steering_server.py``."""

# pylint: disable=missing-function-docstring,import-error

import asyncio
import json
import typing
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from agents_conversation import ITEMS, PUBLISHED
from sessions_database import Backend
from sessions_object import Host, Send, request

from restate import sessions
from restate.agents import _messages
from restate.agents._api import Steer
from restate.agents._common import Check
from restate.agents._signals import with_companion
from restate.agents._conversation import SCHEMA, STEERED, Conversation, answer_once, steer_stored
from restate.agents._steering import take_steers
from restate.agents._turn import _INBOX, _STOP, Inbox, Stop
from restate.sessions.internal import Schema, invoke


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

Run = Callable[[sessions.SessionContext, Conversation], Awaitable[str]]


class Asker:
    """An object whose handler answers each prompt with ``run``, steered with what ``inbox`` holds."""

    def __init__(self, run: Run, *, window: int | None = None, check: Check | None = None):
        self.obj = sessions.SessionObject("asker")
        with_companion(self.obj)  # the companion it calls, as an agent's
        self.backend = Backend()
        self.inbox: list[Steer] = []
        self.hosts: list[Host] = []

        @self.obj.handler
        async def ask(ctx: sessions.SessionContext, text: str) -> str:
            # As ``chat_turn`` sets it, from what the companion's ``start`` returned (each publish answers the same).
            _INBOX.set(ctx, Inbox(revision=1, messages=list(self.inbox)))

            async def ran(conversation: Conversation) -> str:
                return await run(ctx, conversation)

            return await answer_once(ctx, text, ITEMS, ran, window, check=check)

        @self.obj.view
        async def messages(ctx: sessions.ViewContext, wanted: Any = None) -> Any:
            return await _messages.page(ctx.db, ITEMS, wanted)

    async def ask(self, text: str, invocation: Any = None) -> Any:
        host = Host(self.backend, service="asker", answers=PUBLISHED)
        self.hosts.append(host)
        return await invoke(self.obj, "ask", host, invocation or request(), text)

    async def messages(self, wanted: Any = None) -> Any:
        return await invoke(self.obj, "messages", Host(self.backend, service="asker").view(), request(), wanted)

    def taken(self) -> list[Send]:
        return [send for host in self.hosts for send in host.sends if send.handler == "taken"]


async def tool_round(conversation: Conversation, call_id: str = "c1") -> None:
    """The model's reply with one tool call, and the call's result: the turn's next step is a boundary."""
    call = {"id": call_id, "name": "lookup", "input": {}}
    await conversation.turn.append_reply({"role": "assistant", "text": "", "calls": [call]})
    await conversation.turn.append({"result": call_id, "output": "found"})


def texts(items: list[Any]) -> list[str]:
    return [item["text"] for item in items]


async def test_a_steered_message_is_stored_once_as_a_step_of_the_turn_and_taken_with_its_store():
    took: list[list[str]] = []
    seen: list[list[Any]] = []

    async def run(ctx: sessions.SessionContext, conversation: Conversation) -> str:
        await tool_round(conversation)
        took.append(texts(await take_steers(ctx, conversation)))
        # A later boundary, with the same message still listed: it is not taken again.
        took.append(texts(await take_steers(ctx, conversation)))
        seen.append(conversation.history())
        turn = conversation.turn
        assert [entry.steer for entry in turn.steered()] == ["s1"]
        # The transcript's entry of the turn has it too, as the next turn loads it.
        assert [entry.steer for entry in turn.prompt.steers] == ["s1"]
        # A steered message asks for no answer of its own: the turn goes on from its prompt.
        assert turn.latest_ask() is turn.prompt
        await turn.append_reply({"role": "assistant", "text": "done, in Celsius"})
        return "done, in Celsius"

    asker = Asker(run)
    asker.inbox = [{"id": "s1", "text": "use Celsius"}]
    assert await asker.ask("the weather?") == "done, in Celsius"
    assert took == [["use Celsius"], []]
    # The model's next request has the message, after the tool's result.
    assert seen[0][-1] == {"role": "user", "text": "use Celsius"}
    assert seen[0][-2] == {"result": "c1", "output": "found"}
    # The transaction that stored it sent the companion its id, once.
    [sent] = asker.taken()
    assert (sent.service, json.loads(sent.arg), sent.key, sent.idempotency_key) == (
        "asker.agentprebuilt.restate",
        ["s1"],
        "alice",
        "taken:s1",
    )


async def test_no_message_is_taken_while_a_tool_call_has_no_result_nor_outside_a_chat_turn():
    took: list[list[Any]] = []

    async def run(ctx: sessions.SessionContext, conversation: Conversation) -> str:
        call = {"id": "c1", "name": "lookup", "input": {}}
        await conversation.turn.append_reply({"role": "assistant", "text": "", "calls": [call]})
        took.append(await take_steers(ctx, conversation))
        await conversation.turn.append({"result": "c1", "output": "found"})
        _INBOX.delete(ctx)
        took.append(await take_steers(ctx, conversation))
        await conversation.turn.append_reply({"role": "assistant", "text": "done"})
        return "done"

    asker = Asker(run)
    asker.inbox = [{"id": "s1", "text": "use Celsius"}]
    assert await asker.ask("the weather?") == "done"
    assert took == [[], []]
    assert asker.taken() == []


async def test_a_retried_attempt_finds_the_steered_message_stored_and_never_takes_it_again():
    attempts: list[list[str]] = []

    async def run(ctx: sessions.SessionContext, conversation: Conversation) -> str:
        if not conversation.turn.items:
            await tool_round(conversation)
        took = await take_steers(ctx, conversation)
        attempts.append([entry.steer or "" for entry in conversation.turn.steered()])
        if len(attempts) == 1:
            assert texts(took) == ["use Celsius"]
            raise RuntimeError("the process crashed after storing the message")
        assert took == []
        await conversation.turn.append_reply({"role": "assistant", "text": "done"})
        return "done"

    asker, invocation = Asker(run), request()
    asker.inbox = [{"id": "s1", "text": "use Celsius"}]
    with pytest.raises(RuntimeError, match="crashed"):
        await asker.ask("the weather?", invocation)
    assert await asker.ask("the weather?", invocation) == "done"
    # The retry loaded it as stored, marked as steered.
    assert attempts == [["s1"], ["s1"]]
    assert len(asker.taken()) == 1


async def test_an_earlier_turn_reaches_the_model_as_its_prompt_its_steered_messages_and_its_answer():
    seen: list[list[Any]] = []

    async def run(ctx: sessions.SessionContext, conversation: Conversation) -> str:
        seen.append(conversation.history())
        if conversation.turn.prompt.item["text"] == "first":
            await tool_round(conversation)
            await take_steers(ctx, conversation)
        await conversation.turn.append_reply({"role": "assistant", "text": "ok"})
        return "ok"

    # A window of 0: every earlier turn is its messages and answer.
    asker = Asker(run, window=0)
    asker.inbox = [{"id": "s1", "text": "and then?"}]
    await asker.ask("first")
    asker.inbox = []
    await asker.ask("second")
    assert seen[1] == [
        {"role": "user", "text": "first"},
        {"role": "user", "text": "and then?"},
        {"role": "assistant", "text": "ok"},
        {"role": "user", "text": "second"},
    ]


async def test_a_turn_counts_its_steered_messages_toward_the_budget_as_soon_as_it_is_answered():
    loaded: list[Conversation] = []

    async def run(ctx: sessions.SessionContext, conversation: Conversation) -> str:
        loaded.append(conversation)
        await tool_round(conversation)
        await take_steers(ctx, conversation)
        await conversation.turn.append_reply({"role": "assistant", "text": "done"})
        return "done"

    # A window of 0: the turn just answered is before the next turn's window.
    asker = Asker(run, window=0)
    asker.inbox = [{"id": "s1", "text": "use Celsius"}]
    await asker.ask("the weather?")
    [conversation] = loaded
    # Its message and answer (16 characters) fit in 20; with the steered message (11 more), they do not: the
    # summary due now is asked for now, rather than after the next turn.
    conversation.budget = 5
    assert conversation.overflowing()


async def test_a_turn_stopped_while_it_waits_to_store_steered_messages_takes_none():
    async def run(ctx: sessions.SessionContext, conversation: Conversation) -> str:
        await tool_round(conversation)
        stop = Stop()
        _STOP.set(ctx, stop)
        # Another write holds the ledger: the turn waits for it to store the message, and stops meanwhile.
        async with conversation._ledger.lock:  # pylint: disable=protected-access
            taking = asyncio.ensure_future(take_steers(ctx, conversation))
            for _ in range(10):
                await asyncio.sleep(0)
            stop.stop(RuntimeError("stopped"))
        assert await taking == []
        # Not the turn's: its agent's ``steered`` answers it as a turn of its own.
        assert not await steer_stored(ctx.db, "s1")
        assert conversation.turn.steered() == []
        _STOP.delete(ctx)
        await conversation.turn.append_reply({"role": "assistant", "text": "done"})
        return "done"

    asker = Asker(run)
    asker.inbox = [{"id": "s1", "text": "use Celsius"}]
    assert await asker.ask("the weather?") == "done"
    assert asker.taken() == []


async def test_the_output_check_asks_again_once_after_a_steered_message():
    replies = iter(["prose", "still prose"])
    asked: list[str] = []

    async def run(ctx: sessions.SessionContext, conversation: Conversation) -> str:
        turn = conversation.turn
        if not turn.items:
            await tool_round(conversation)
            await take_steers(ctx, conversation)
        asked.append(turn.latest_ask().item["text"])
        reply = next(replies)
        await turn.append_reply({"role": "assistant", "text": reply})
        return reply

    asker = Asker(run, check=lambda answer, _: "Reply again, with JSON only." if answer == "prose" else None)
    asker.inbox = [{"id": "s1", "text": "be brief"}]
    assert await asker.ask("plan") == "still prose"
    # The steered message did not count as the ask: the check asked again, once.
    assert asked == ["plan", "Reply again, with JSON only."]


async def test_a_dropped_reply_drops_the_message_steered_after_it_which_is_then_free_to_be_answered():
    async def run(ctx: sessions.SessionContext, conversation: Conversation) -> str:
        turn = conversation.turn
        await tool_round(conversation)
        await take_steers(ctx, conversation)
        assert await steer_stored(ctx.db, "s1")
        [reply] = turn.replies()
        await turn.drop(reply)
        assert turn.steered() == []
        assert turn.prompt.steers == []
        assert not await steer_stored(ctx.db, "s1")
        [[rows]] = await ctx.db.values(f"SELECT COUNT(*) FROM {STEERED}")
        assert rows == 0
        await turn.append_reply({"role": "assistant", "text": "again"})
        return "again"

    asker = Asker(run)
    asker.inbox = [{"id": "s1", "text": "use Celsius"}]
    assert await asker.ask("the weather?") == "again"


async def test_the_summary_view_shows_a_steered_message_in_its_turn_once_from_a_cursor():
    async def run(ctx: sessions.SessionContext, conversation: Conversation) -> str:
        await tool_round(conversation)
        await take_steers(ctx, conversation)
        await conversation.turn.append_reply({"role": "assistant", "text": "done"})
        return "done"

    asker = Asker(run)
    asker.inbox = [{"id": "s1", "text": "use Celsius"}]
    await asker.ask("the weather?")
    page = await asker.messages({"view": "summary"})
    shown = [(m["role"], m["parts"][0]["text"]) for m in page["messages"]]
    assert shown == [("user", "the weather?"), ("user", "use Celsius"), ("assistant", "done")]
    # The full view shows it within the turn, between the tool's call and the answer.
    full = await asker.messages()
    assert [m["role"] for m in full["messages"]] == ["user", "assistant", "user", "assistant"]
    # From a cursor before it, it comes once; from the cursor after it, not again.
    since = await asker.messages({"view": "summary", "cursor": 1})
    assert ("user", "use Celsius") in [(m["role"], m["parts"][0]["text"]) for m in since["messages"]]
    after = await asker.messages({"view": "summary", "cursor": since["cursor"]})
    assert after["messages"] == []


async def test_a_database_of_the_schema_before_steering_takes_its_table_as_it_opens():
    before = Schema(SCHEMA.name, list(SCHEMA.steps[:-1]))
    obj = sessions.SessionObject("asker")
    backend = Backend()

    @obj.handler
    async def old(ctx: sessions.SessionContext) -> None:
        await before.ensure(ctx.db)

    @obj.handler
    async def new(ctx: sessions.SessionContext) -> bool:
        return await steer_stored(ctx.db, "s1")

    await invoke(obj, "old", Host(backend, service="asker"), request())
    assert await invoke(obj, "new", Host(backend, service="asker"), request()) is False


async def test_steer_dedupes_by_id_within_the_messages_given():
    async def run(ctx: sessions.SessionContext, conversation: Conversation) -> str:
        await tool_round(conversation)
        took = await take_steers(ctx, conversation)
        assert texts(took) == ["once"]
        await conversation.turn.append_reply({"role": "assistant", "text": "done"})
        return "done"

    asker = Asker(run)
    asker.inbox = typing.cast(list[Steer], [{"id": "s1", "text": "once"}, {"id": "s1", "text": "once"}])
    assert await asker.ask("hi") == "done"
