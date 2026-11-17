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
"""An agent's turn that waits for an approval, which Restate shut down meanwhile, and is then stopped."""

# pylint: disable=missing-function-docstring

import asyncio
import typing
import uuid
from datetime import timedelta
from typing import Any

import httpx2
import pydantic
import pytest

import restate
from agents_conversation import ITEMS  # pylint: disable=import-error
from restate import HarnessEnvironment, sessions
from restate.agents._conversation import Conversation, answer_once
from restate.agents._object import AgentObject
from restate.agents._tools import ToolCall, run_tool, wait_for_signal
from support import RESTATE_IMAGE  # pylint: disable=import-error


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]


class Refund(pydantic.BaseModel):
    order: str
    amount: int


approval = wait_for_signal("approval", description="Wait for a manager to approve a refund.", input=Refund)


async def answer(ctx: sessions.SessionContext, text: str) -> str:
    """A scripted framework whose model asks for an approval, and waits for it."""

    async def work(conversation: Conversation) -> str:
        call_id = "call-1"
        args = {"order": "1", "amount": 5}
        call = {"id": call_id, "name": approval.name, "input": args}
        await conversation.turn.append_reply({"role": "assistant", "text": "", "calls": [call]})
        result = await run_tool(approval, args, ToolCall(ctx, call_id))
        await conversation.turn.append({"result": call_id, "output": result})
        await conversation.turn.append_reply({"role": "assistant", "text": f"refund: {result}"})
        return f"refund: {result}"

    return await answer_once(ctx, text, ITEMS, work)


# Restate shuts down an attempt it heard nothing from for a second.
clerk = AgentObject("clerk", inactivity_timeout=timedelta(seconds=1))
clerk.chat(answer, items=ITEMS)


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(restate.app([clerk]), restate_image=RESTATE_IMAGE) as harness:
        yield harness


async def test_stop_ends_a_turn_that_restate_shut_down_while_it_waited_for_an_approval(harness: HarnessEnvironment):
    key = str(uuid.uuid4())
    seen: list[str] = []
    async with httpx2.AsyncClient(timeout=30) as http:
        sent = await http.post(
            f"{harness.ingress_url}/clerk/{key}/chat/send", content="refund", headers={"content-type": "text/plain"}
        )
        invocation_id = typing.cast(str, sent.json()["invocationId"])

        async def status() -> str | None:
            answered = await http.post(
                f"{harness.admin_api_url}/query",
                json={"query": f"SELECT status FROM sys_invocation WHERE id = '{invocation_id}'"},
                headers={"Accept": "application/json"},
            )
            rows = typing.cast("list[dict[str, Any]]", answered.json().get("rows", []))
            return typing.cast(str, rows[0]["status"]) if rows else None

        for _ in range(100):
            now = await status()
            if now is not None and (not seen or seen[-1] != now):
                seen.append(now)
            if now == "suspended":
                break
            await asyncio.sleep(0.1)
        assert seen and seen[-1] == "suspended", seen
        stopped = await http.post(f"{harness.ingress_url}/clerk/{key}/stop", json=None)
        seen.append(f"stop: {stopped.status_code} {stopped.text[:80]}")
        try:
            answered = await http.get(f"{harness.ingress_url}/restate/invocation/{invocation_id}/attach", timeout=10)
            code: int | None = answered.status_code
        except httpx2.TimeoutException:
            code = None
        seen.append(f"after the stop: {await status()}")
        if code is None:
            await http.patch(f"{harness.admin_api_url}/invocations/{invocation_id}/kill")
    assert code == 409, f"the stopped turn did not end in 10 s: {seen}"
