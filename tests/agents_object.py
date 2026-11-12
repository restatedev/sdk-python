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
import signal
import subprocess
import sys
import time
import types
import typing
import uuid
from collections.abc import Awaitable, Callable

import httpx2
import pydantic
from agents_conversation import ITEMS  # pylint: disable=import-error
import pytest

import restate
from support import RESTATE_IMAGE, Gate  # pylint: disable=import-error
from restate import HarnessEnvironment, sessions
from restate.agents._api import Messages, Status
from restate.agents._object import AgentObject
from restate.agents._tools import ToolCall, run_tool, wait_for_signal
from restate.agents._conversation import Conversation, answer_once
from restate.agents._turn import stop_signal_of
from restate.agents import __main__ as cli
from restate.agents.client import AgentClient, Followed
from restate.client import create_client


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

# A scripted framework, on the made-up items of agents_conversation: it answers with an echo.
gate = Gate()
forgotten: list[str] = []


async def answer(ctx: sessions.SessionContext, text: str) -> str:
    async def work(conversation: Conversation) -> str:
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
            call = {"id": call_id, "name": approval.name, "input": args}
            await conversation.turn.append_reply({"role": "assistant", "text": "", "calls": [call]})
            result = await run_tool(approval, args, ToolCall(ctx, call_id))
            await conversation.turn.append({"result": call_id, "output": result})
            await conversation.turn.append_reply({"role": "assistant", "text": f"refund: {result}"})
            return f"refund: {result}"
        await conversation.turn.append_reply({"role": "assistant", "text": f"echo: {text}"})
        return f"echo: {text}"

    return await answer_once(ctx, text, ITEMS, work)


async def forget(ctx: sessions.SessionContext) -> None:
    forgotten.append(ctx.key)


assistant = AgentObject("assistant")
assistant.chat(answer, items=ITEMS, clear=forget)

other = AgentObject("other")
"""A second agent in the same app, with a companion of its own."""
other.chat(answer, items=ITEMS)


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(restate.app([assistant, other]), restate_image=RESTATE_IMAGE) as harness:
        yield harness


TEXT = {"content-type": "text/plain"}
"""A message as text, as `curl -d` sends one."""


async def call(harness: HarnessEnvironment, handler: str, key: str, body: object = None) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=60) as ingress:
        return await ingress.post(f"{harness.ingress_url}/assistant/{key}/{handler}", json=body)


async def send(harness: HarnessEnvironment, key: str, text: str) -> str:
    async with httpx2.AsyncClient(timeout=60) as ingress:
        sent = await ingress.post(f"{harness.ingress_url}/assistant/{key}/chat/send", content=text, headers=TEXT)
        return typing.cast(str, sent.json()["invocationId"])


async def attach(harness: HarnessEnvironment, invocation_id: str) -> httpx2.Response:
    async with httpx2.AsyncClient(timeout=60) as ingress:
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
    async with httpx2.AsyncClient(timeout=60) as ingress:
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


async def test_the_views_without_input_take_a_request_with_a_json_content_type(harness: HarnessEnvironment):
    # Restate's TypeScript client sends `content-type: application/json` with every call, an argument or not.
    key = str(uuid.uuid4())
    async with httpx2.AsyncClient(timeout=60) as ingress:
        for view in ("status", "pending", "notifications", "stop", "clear"):
            response = await ingress.post(
                f"{harness.ingress_url}/assistant/{key}/{view}",
                content=b"",
                headers={"content-type": "application/json"},
            )
            assert response.status_code == 200, (view, response.text)
        # And without a body, as `curl` sends it.
        assert (await ingress.post(f"{harness.ingress_url}/assistant/{key}/status")).status_code == 200


async def test_status_says_which_message_is_answered_and_stop_cancels_it(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    gate.close()
    invocation = await send(harness, key, "wait")

    async def answering() -> bool:
        status = typing.cast(Status, (await call(harness, "status", key)).json())
        return status["answering"] is not None and status["answering"]["invocationId"] == invocation

    await until(answering)
    assert (await call(harness, "stop", key)).json() == {"stopped": invocation}
    assert (await attach(harness, invocation)).status_code == 409
    assert (await call(harness, "status", key)).json()["answering"] is None
    # The next message is answered.
    gate.open()
    assert (await call(harness, "chat", key, "next")).json() == "echo: next"


async def test_each_agent_has_its_own_companion_which_a_turn_of_the_other_does_not_touch(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    async with httpx2.AsyncClient(timeout=60) as admin:
        served = {
            service["name"] for service in (await admin.get(f"{harness.admin_api_url}/services")).json()["services"]
        }
    assert {"assistant.agentprebuild.restate", "other.agentprebuild.restate"} <= served and "signals" not in served
    # A turn of the assistant, which waits for an approval at the same key.
    invocation = await send(harness, key, "refund o7")

    async def waits() -> bool:
        return len((await call(harness, "pending", key)).json()) == 1

    await until(waits)
    assert (await call(harness, "notifications", key)).json()["revision"] > 0
    async with httpx2.AsyncClient(timeout=60) as ingress:
        other_status = (await ingress.post(f"{harness.ingress_url}/other/{key}/status")).json()
        other_revision = (await ingress.post(f"{harness.ingress_url}/other/{key}/notifications")).json()
        unresolved = (await ingress.post(f"{harness.ingress_url}/other/{key}/signal", json={"name": "approval"})).json()
    # The other agent's companion, at the same key, knows nothing of it, and has no wait to resolve.
    assert other_status == {"answering": None, "waiting": []} and other_revision == {"revision": 0}
    assert unresolved == {"resolved": None}
    await call(harness, "signal", key, {"name": "approval", "value": "approved"})
    assert (await attach(harness, invocation)).json() == "refund: approved"


async def test_clear_forgets_the_conversation_and_its_ids_go_on(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    await call(harness, "chat", key, "one")
    assert (await call(harness, "clear", key)).json() == {"stopped": None}
    cleared = (await call(harness, "messages", key, {})).json()
    assert cleared["messages"] == [] and cleared["first"] == 2
    assert key in forgotten  # what the agent keeps outside its database is cleared too
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


# --- The agent client and the command line --------------------------------------------------------


async def test_the_agent_client_chats_reads_follows_and_resolves_waits(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    async with create_client(harness.ingress_url) as restate_client:
        alice = AgentClient(restate_client, "assistant", key)
        assert await alice.chat("Hi") == "echo: Hi"
        page = await alice.messages(limit=10)
        assert texts(page) == [(0, "user", "Hi"), (1, "assistant", "echo: Hi")]

        following = alice.follow(wait=10)
        first = await anext(following)
        assert [m["id"] for m in first.messages] == [0, 1] and first.status["answering"] is None
        invocation = await alice.send("refund o9")
        # The next changes: the tool call, then its wait, which the client resolves.
        seen: list[Followed] = []
        while not any(change.waiting for change in seen):
            seen.append(await anext(following))
        assert (await alice.pending())[0]["name"] == "approval"
        assert (await alice.signal("approval", "approved")) == f"{invocation}:call-o9"
        while not any(m["parts"][0].get("text") == "refund: approved" for change in seen for m in change.messages):
            seen.append(await anext(following))
        await following.aclose()

        assert await alice.signal("approval", "again") is None
        assert await alice.stop() is None
        assert await alice.clear() is None
        cleared = await alice.messages()
        assert cleared["messages"] == [] and cleared.get("first", 0) > 0


async def test_the_command_line_sends_what_you_type_and_shows_the_answers(
    harness: HarnessEnvironment, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    key = str(uuid.uuid4())
    lines = iter(["Hi\n", "/status\n"])

    def readline() -> str:
        line = next(lines, None)
        if line is None:
            time.sleep(3)  # as a person who reads the answer before leaving (Ctrl-D)
            return ""
        return line

    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(readline=readline))
    await cli.chat(f"assistant/{key}", harness.ingress_url)
    shown = capsys.readouterr().out
    assert "assistant: echo: Hi" in shown and '"answering"' in shown


async def test_the_command_line_says_when_it_cannot_follow_the_conversation(
    harness: HarnessEnvironment, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    def readline() -> str:
        time.sleep(2)
        return ""

    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(readline=readline))
    await cli.chat("nobody/alice", harness.ingress_url)
    assert "Following the conversation failed: HTTP 404" in capsys.readouterr().out


async def test_the_command_line_leaves_on_ctrl_c_while_it_waits_for_a_line(harness: HarnessEnvironment):
    chat = subprocess.Popen(
        [sys.executable, "-m", "restate.agents", "chat", f"assistant/{uuid.uuid4()}", "--ingress", harness.ingress_url],
        stdin=subprocess.PIPE,  # stays open: nothing typed, no Ctrl-D
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert chat.stdout is not None and chat.stdout.readline().startswith("commands:")
        chat.send_signal(signal.SIGINT)
        assert chat.wait(timeout=5) == 0
    finally:
        chat.kill()


def test_the_command_line_takes_an_agent_and_a_key():
    with pytest.raises(SystemExit):
        cli.main(["chat", "assistant"])
