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
"""Steering an agent's turn, with a scripted framework, against a real Restate server: a message sent to ``steer``
joins the turn at its next step, or is answered as a turn of its own, once, in the order it was sent."""

# pylint: disable=missing-function-docstring,import-error

import asyncio
import typing
import uuid
from collections.abc import Awaitable, Callable

import httpx2
import pytest
from agents_conversation import ITEMS
from support import RESTATE_IMAGE, Gate

import restate
from restate import HarnessEnvironment, sessions
from restate.agents._api import Messages, Status
from restate.agents._conversation import Conversation, answer_once
from restate.agents._object import AgentObject
from restate.agents._steering import take_steers
from restate.agents._turn import stop_signal_of, tool_call_ended, tool_call_started


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

gate = Gate()
requests: dict[str, list[list[str]]] = {}
"""What each key's model was asked, request by request: the texts of the messages of the user."""


def framework(name: str, steers: bool) -> AgentObject:
    """An agent whose scripted model calls one tool for ``look ...``, which waits on ``gate``, then answers with
    the messages of the user it was given since the prompt; ``wait`` waits on ``gate`` and echoes."""

    async def answer(ctx: sessions.SessionContext, text: str) -> str:
        async def work(conversation: Conversation) -> str:
            turn = conversation.turn
            if text.startswith("look ") and not turn.items:
                call = {"id": "call-1", "name": "lookup", "input": {}}
                await turn.append_reply({"role": "assistant", "text": "", "calls": [call]})
                await tool_call_started(ctx)
                stop = stop_signal_of(ctx)
                assert stop is not None
                await stop.guard(gate.wait())
                await turn.append({"result": "call-1", "output": "found"})
                await tool_call_ended(ctx)
            elif text == "wait":
                stop = stop_signal_of(ctx)
                assert stop is not None
                await stop.guard(gate.wait())
            # The step before the model's next request: it takes the messages steered to the turn.
            await take_steers(ctx, conversation)
            history = conversation.history()
            asked = [item["text"] for item in history if item.get("role") == "user"]
            requests.setdefault(ctx.key, []).append(asked)
            heard = [entry.item["text"] for entry in turn.steered()]
            reply = f"{text}: {', '.join(heard)}" if heard else f"echo: {text}"
            await turn.append_reply({"role": "assistant", "text": reply})
            return reply

        return await answer_once(ctx, text, ITEMS, work)

    agent = AgentObject(name)
    agent.chat(answer, items=ITEMS, steers=steers)
    return agent


steerer = framework("steerer", steers=True)
plain = framework("plain", steers=False)


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(restate.app([steerer, plain]), restate_image=RESTATE_IMAGE) as harness:
        yield harness


TEXT = {"content-type": "text/plain"}


async def post(harness: HarnessEnvironment, path: str, body: object = None, **headers: str) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=60) as ingress:
        if isinstance(body, str):
            return await ingress.post(f"{harness.ingress_url}/{path}", content=body, headers={**TEXT, **headers})
        return await ingress.post(f"{harness.ingress_url}/{path}", json=body, headers=headers)


async def send(harness: HarnessEnvironment, agent: str, key: str, text: str) -> str:
    sent = await post(harness, f"{agent}/{key}/chat/send", text)
    return typing.cast(str, sent.json()["invocationId"])


async def steer(harness: HarnessEnvironment, agent: str, key: str, text: str) -> dict[str, typing.Any]:
    # With an idempotency key, as a client sends it: a retried request is the same message.
    steered = await post(harness, f"{agent}/{key}/steer", text, **{"idempotency-key": str(uuid.uuid4())})
    assert steered.status_code == 200, steered.text
    return typing.cast(dict[str, typing.Any], steered.json())


async def attach(harness: HarnessEnvironment, invocation_id: str) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=60) as ingress:
        return await ingress.get(f"{harness.ingress_url}/restate/invocation/{invocation_id}/attach")


async def status(harness: HarnessEnvironment, agent: str, key: str) -> Status:
    return typing.cast(Status, (await post(harness, f"{agent}/{key}/status")).json())


async def until(check: Callable[[], Awaitable[bool]]) -> None:
    for _ in range(300):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("never")


async def answering(harness: HarnessEnvironment, agent: str, key: str, invocation: str) -> None:
    async def started() -> bool:
        turn = (await status(harness, agent, key))["answering"]
        return turn is not None and turn["invocationId"] == invocation

    await until(started)


async def summary(harness: HarnessEnvironment, agent: str, key: str) -> list[tuple[str, str]]:
    page = typing.cast(Messages, (await post(harness, f"{agent}/{key}/messages", {"view": "summary"})).json())
    return [(m["role"], typing.cast(str, m["parts"][0].get("text"))) for m in page["messages"]]


async def settled(harness: HarnessEnvironment, agent: str, key: str) -> None:
    """Wait until the key answered every message queued so far: a chat sent now is answered after them."""
    async with httpx2.AsyncClient(timeout=60) as ingress:
        done = await ingress.post(f"{harness.ingress_url}/{agent}/{key}/chat", content="done", headers=TEXT)
        assert done.status_code == 200, done.text


async def test_a_message_steered_during_a_tool_call_joins_the_turn_at_its_next_step(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    gate.close()
    invocation = await send(harness, "steerer", key, "look up the weather")
    await answering(harness, "steerer", key, invocation)
    steered = await steer(harness, "steerer", key, "use Celsius")
    assert steered["turn"] == invocation
    assert [one["id"] for one in (await status(harness, "steerer", key)).get("steering", [])] == [steered["id"]]
    gate.open()
    answer = await attach(harness, invocation)
    assert answer.json() == "look up the weather: use Celsius"
    # The model's request after the tool's result had the message.
    assert requests[key][-1] == ["look up the weather", "use Celsius"]

    async def taken() -> bool:
        return "steering" not in await status(harness, "steerer", key)

    await until(taken)
    # Its queued turn of its own did nothing: the turn took it. The summary shows it in the turn.
    await settled(harness, "steerer", key)
    assert await summary(harness, "steerer", key) == [
        ("user", "look up the weather"),
        ("user", "use Celsius"),
        ("assistant", "look up the weather: use Celsius"),
        ("user", "done"),
        ("assistant", "echo: done"),
    ]


async def test_a_message_steered_after_the_last_step_is_answered_next_in_the_order_it_was_sent(
    harness: HarnessEnvironment,
):
    key = str(uuid.uuid4())
    gate.close()
    invocation = await send(harness, "steerer", key, "wait")
    await answering(harness, "steerer", key, invocation)
    # The turn reaches no step after them: the steered message is answered next, then the chat sent after it.
    assert (await steer(harness, "steerer", key, "and also"))["turn"] == invocation
    after = await send(harness, "steerer", key, "then this")
    gate.open()
    assert (await attach(harness, invocation)).json() == "echo: wait"
    assert (await attach(harness, after)).json() == "echo: then this"
    assert await summary(harness, "steerer", key) == [
        ("user", "wait"),
        ("assistant", "echo: wait"),
        ("user", "and also"),
        ("assistant", "echo: and also"),
        ("user", "then this"),
        ("assistant", "echo: then this"),
    ]


async def test_a_message_steered_with_no_turn_being_answered_starts_one(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    gate.open()
    assert (await steer(harness, "steerer", key, "hello"))["turn"] is None
    await settled(harness, "steerer", key)
    assert await summary(harness, "steerer", key) == [
        ("user", "hello"),
        ("assistant", "echo: hello"),
        ("user", "done"),
        ("assistant", "echo: done"),
    ]


async def test_an_agent_that_takes_no_steered_messages_answers_each_as_a_turn_of_its_own(
    harness: HarnessEnvironment,
):
    key = str(uuid.uuid4())
    gate.close()
    invocation = await send(harness, "plain", key, "look up the weather")
    await answering(harness, "plain", key, invocation)
    assert (await steer(harness, "plain", key, "use Celsius"))["turn"] is None
    gate.open()
    assert (await attach(harness, invocation)).json() == "echo: look up the weather"
    await settled(harness, "plain", key)
    assert await summary(harness, "plain", key) == [
        ("user", "look up the weather"),
        ("assistant", "echo: look up the weather"),
        ("user", "use Celsius"),
        ("assistant", "echo: use Celsius"),
        ("user", "done"),
        ("assistant", "echo: done"),
    ]


async def test_a_message_steered_to_a_turn_killed_before_its_next_step_is_answered_once(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    gate.close()
    invocation = await send(harness, "steerer", key, "look twice")
    await answering(harness, "steerer", key, invocation)
    await steer(harness, "steerer", key, "first steer")
    gate.open()
    assert (await attach(harness, invocation)).json() == "look twice: first steer"
    # A second turn is killed while its tool runs, before the step at which it would take the message.
    gate.close()
    killed = await send(harness, "steerer", key, "look again")
    await answering(harness, "steerer", key, killed)
    await steer(harness, "steerer", key, "second steer")
    async with httpx2.AsyncClient(timeout=60) as admin:
        response = await admin.patch(f"{harness.admin_api_url}/invocations/{killed}/kill")
        assert response.status_code < 300, response.text
    gate.open()
    await settled(harness, "steerer", key)
    shown = await summary(harness, "steerer", key)
    # The killed turn never took the message: it is answered once, as a turn of its own.
    assert [text for role, text in shown if role == "user"].count("second steer") == 1
    assert ("assistant", "echo: second steer") in shown
