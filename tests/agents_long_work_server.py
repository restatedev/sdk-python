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
"""An agent's turn whose model takes longer than Restate's inactivity timeout, against a real server."""

# pylint: disable=missing-function-docstring

import asyncio
import typing
import uuid
from datetime import timedelta

import httpx2
import pytest

import restate
from agents_conversation import ITEMS  # pylint: disable=import-error
from restate import HarnessEnvironment, sessions
from restate.agents._conversation import Conversation, answer_once
from restate.agents._object import AgentObject
from restate.sessions import _driver
from support import RESTATE_IMAGE  # pylint: disable=import-error


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

requests: dict[str, int] = {}
"""How many times the model was asked, by the conversation's key."""


async def answer(ctx: sessions.SessionContext, text: str) -> str:
    """A scripted framework whose model thinks for longer than the inactivity timeout, and says nothing meanwhile.

    It is what a turn of any framework is while its model request runs: local work, with nothing of Restate's
    outstanding.
    """

    async def work(conversation: Conversation) -> str:
        requests[ctx.key] = requests.get(ctx.key, 0) + 1
        await asyncio.sleep(3)
        await conversation.turn.append_reply({"role": "assistant", "text": f"echo: {text}"})
        return f"echo: {text}"

    return await answer_once(ctx, text, ITEMS, work)


thinker = AgentObject("thinker", inactivity_timeout=timedelta(seconds=1))
thinker.chat(answer, items=ITEMS)


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(restate.app([thinker]), restate_image=RESTATE_IMAGE) as harness:
        yield harness


async def test_a_model_request_longer_than_the_inactivity_timeout_is_made_once_and_answered(
    harness: HarnessEnvironment, monkeypatch
):
    # The attempt heartbeats while the model thinks, faster than the agent's inactivity timeout of a second.
    monkeypatch.setattr(_driver, "HEARTBEAT_INTERVAL", timedelta(seconds=0.3))
    key = str(uuid.uuid4())
    async with httpx2.AsyncClient(timeout=30) as ingress:
        sent = await ingress.post(
            f"{harness.ingress_url}/thinker/{key}/chat/send", content="hello", headers={"content-type": "text/plain"}
        )
        invocation_id = typing.cast(str, sent.json()["invocationId"])
        try:
            answered = await ingress.get(f"{harness.ingress_url}/restate/invocation/{invocation_id}/attach", timeout=20)
        except httpx2.TimeoutException:
            answered = None
        finally:
            async with httpx2.AsyncClient() as admin:
                await admin.patch(f"{harness.admin_api_url}/invocations/{invocation_id}/kill")
    assert answered is not None, f"the turn did not answer in 20 s; the model was asked {requests.get(key, 0)} times"
    assert answered.status_code == 200, answered.text
    assert requests.get(key) == 1
