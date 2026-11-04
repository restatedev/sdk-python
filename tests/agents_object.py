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
"""An agent object and its turns, with a scripted framework, against a real Restate server."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import asyncio
import typing
import uuid
from collections.abc import Awaitable, Callable

import httpx
import pydantic
import pytest

import restate
from restate import HarnessEnvironment, sessions
from restate.agents._api import Messages, Status
from restate.agents._object import AgentObject
from restate.agents._tools import ToolCall, run_tool, wait_for_signal
from restate.agents._transcript import Transcript
from restate.agents._turn import stop_signal_of, transcribing


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]


class AuthenticationError(Exception):
    """As a model SDK names the error of a missing or wrong API key."""


class Refund(pydantic.BaseModel):
    order: str
    amount: float


approval = wait_for_signal("approval", description="Wait for a manager to approve a refund.", input=Refund)

# A scripted framework: it keeps each key's transcript in this process, and answers with an echo.
transcripts: dict[str, Transcript] = {}
gate = asyncio.Event()


async def answer(ctx: sessions.SessionContext, text: str) -> str:
    transcript = transcripts.setdefault(ctx.key, Transcript())
    transcript.say("user", text)

    async def read() -> Transcript:
        return transcript

    async def work() -> str:
        if text == "wait":
            stop = stop_signal_of(ctx)
            assert stop is not None
            await stop.guard(gate.wait())
        if text == "refuse":
            raise AuthenticationError("invalid x-api-key")
        if text.startswith("refund "):
            # A tool call: the model asks for an approval, and waits for it.
            call_id = f"call-{text.split()[1]}"
            args = {"order": text.split()[1], "amount": 5}
            transcript.call(call_id, approval.name, args)
            result = await run_tool(approval, args, ToolCall(ctx, call_id))
            transcript.result(call_id, result)
            transcript.say("assistant", f"refund: {result}")
            return f"refund: {result}"
        transcript.say("assistant", f"echo: {text}")
        return f"echo: {text}"

    return await transcribing(ctx, read, work)


async def forget(ctx: sessions.SessionContext) -> None:
    transcripts.pop(ctx.key, None)


assistant = AgentObject("assistant")
assistant.chat(answer, clear=forget)


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        sessions.app(assistant), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


TEXT = {"content-type": "text/plain"}
"""A message as text, as `curl -d` sends one."""


async def call(harness: HarnessEnvironment, handler: str, key: str, body: object = None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=60) as ingress:
        return await ingress.post(f"{harness.ingress_url}/assistant/{key}/{handler}", json=body)


async def send(harness: HarnessEnvironment, key: str, text: str) -> str:
    async with httpx.AsyncClient(timeout=60) as ingress:
        sent = await ingress.post(f"{harness.ingress_url}/assistant/{key}/chat/send", content=text, headers=TEXT)
        return typing.cast(str, sent.json()["invocationId"])


async def attach(harness: HarnessEnvironment, invocation_id: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=60) as ingress:
        return await ingress.get(f"{harness.ingress_url}/restate/invocation/{invocation_id}/attach")


async def until(check: Callable[[], Awaitable[bool]]) -> None:
    for _ in range(200):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("never")


def texts(page: Messages) -> list[tuple[int, str, str]]:
    return [
        (message["id"], message["role"], typing.cast(str, message["parts"][0].get("text")))
        for message in page["messages"]
    ]


async def test_a_message_is_answered_and_the_conversation_read_and_followed(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    async with httpx.AsyncClient(timeout=60) as ingress:
        answered = await ingress.post(f"{harness.ingress_url}/assistant/{key}/chat", content="Hi", headers=TEXT)
    assert answered.json() == "echo: Hi"
    page = (await call(harness, "messages", key, {})).json()
    assert texts(page) == [(0, "user", "Hi"), (1, "assistant", "echo: Hi")]
    synced = (await call(harness, "sync", key, {})).json()
    assert synced["revision"] > 0 and synced["status"] == {"answering": None, "waiting": []}
    assert texts(synced) == texts(page)
    # Followed from its cursor, the conversation has nothing newer; the next message is.
    later = (await call(harness, "sync", key, {"cursor": synced["cursor"]})).json()
    assert later["messages"] == []
    await call(harness, "chat", key, "Again")
    newer = (await call(harness, "sync", key, {"cursor": synced["cursor"]})).json()
    assert [m["id"] for m in newer["messages"]] == [2, 3]


async def test_a_sync_that_waited_returns_every_change_its_revision_counts(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    await call(harness, "chat", key, "Hi")
    synced = (await call(harness, "sync", key, {})).json()
    timeout = 20
    started = asyncio.get_running_loop().time()

    async def follow() -> list[int]:
        # As a client follows: each sync waits for a revision newer than the last one it got.
        state, seen = synced, []
        while 3 not in seen:
            request = {"revision": state["revision"], "cursor": state["cursor"], "timeoutSeconds": timeout}
            state = (await call(harness, "sync", key, request)).json()
            seen += [message["id"] for message in state["messages"]]
        return seen

    following = asyncio.ensure_future(follow())
    await asyncio.sleep(0.5)  # the first sync waits
    await call(harness, "chat", key, "Again")
    assert 2 in await following and 3 in following.result()
    # Every message came with the revision that counted it: no sync had to time out to bring it.
    assert asyncio.get_running_loop().time() - started < timeout


async def test_status_says_which_message_is_answered_and_stop_cancels_it(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    gate.clear()
    invocation = await send(harness, key, "wait")

    async def answering() -> bool:
        status = typing.cast(Status, (await call(harness, "status", key)).json())
        return status["answering"] is not None and status["answering"]["invocationId"] == invocation

    await until(answering)
    assert (await call(harness, "stop", key)).json() == {"stopped": invocation}
    assert (await attach(harness, invocation)).status_code == 409
    assert (await call(harness, "status", key)).json()["answering"] is None
    # The next message is answered.
    gate.set()
    assert (await call(harness, "chat", key, "next")).json() == "echo: next"


async def test_clear_forgets_the_conversation_and_its_ids_go_on(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    await call(harness, "chat", key, "one")
    assert (await call(harness, "clear", key)).json() == {"stopped": None}
    cleared = (await call(harness, "messages", key, {})).json()
    assert cleared["messages"] == [] and cleared["first"] == 2
    await call(harness, "chat", key, "two")
    assert texts((await call(harness, "messages", key, {})).json()) == [
        (2, "user", "two"),
        (3, "assistant", "echo: two"),
    ]


async def test_a_refused_message_fails_with_424_and_the_next_is_answered(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    refused = await call(harness, "chat", key, "refuse")
    assert refused.status_code == 424
    assert (await call(harness, "status", key)).json()["answering"] is None
    assert (await call(harness, "chat", key, "fine")).json() == "echo: fine"


async def test_a_tool_waits_for_a_signal_which_the_agents_signal_view_resolves(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    invocation = await send(harness, key, "refund o7")

    async def waiting() -> bool:
        return len((await call(harness, "pending", key)).json()) == 1

    await until(waiting)
    [wait] = (await call(harness, "pending", key)).json()
    assert wait["name"] == "approval" and wait["input"] == {"order": "o7", "amount": 5}
    assert wait["id"] == f"{invocation}:call-o7"
    assert (await call(harness, "signal", key, {"name": "approval", "value": "approved"})).json() == {
        "resolved": wait["id"]
    }
    assert (await attach(harness, invocation)).json() == "refund: approved"
    page = (await call(harness, "messages", key, {})).json()
    [tool_call] = page["messages"][1]["parts"]
    assert tool_call["name"] == "wait_for_approval" and tool_call["output"] == "approved"
    assert (await call(harness, "pending", key)).json() == []
