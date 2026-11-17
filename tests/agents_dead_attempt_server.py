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
"""A dead attempt's model request stops: Restate shuts the attempt down mid-request, and the retry asks again."""

# pylint: disable=missing-function-docstring

import asyncio
import typing
import uuid
from collections import Counter
from datetime import timedelta

import httpx2
import pytest

import restate
from agents_conversation import ITEMS  # pylint: disable=import-error
from restate import HarnessEnvironment, sessions
from restate.agents._conversation import Conversation, answer_once
from restate.agents._object import AgentObject
from restate.agents._turn import Stop, stop_signal_of
from support import RESTATE_IMAGE  # pylint: disable=import-error


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

started: Counter[str] = Counter()
"""How many model requests started, by the conversation's key."""
cancelled: Counter[str] = Counter()
"""How many model requests were cancelled, by the conversation's key."""
completed: Counter[str] = Counter()
"""How many model requests ran to their end, by the conversation's key."""

SLOW = 4.0
"""How long a slow model request takes: longer than the inactivity timeout, shorter than a heartbeat (20 s)."""


async def model(key: str) -> str:
    """A model request: its first two are slow, so that Restate shuts their attempts down; the third is quick."""
    started[key] += 1
    try:
        await asyncio.sleep(SLOW if started[key] < 3 else 0.1)
    except asyncio.CancelledError:
        cancelled[key] += 1
        raise
    completed[key] += 1
    return "done"


async def answer(ctx: sessions.SessionContext, text: str) -> str:
    """A scripted framework whose model request goes through the turn's stop signal, as the frameworks' do."""

    async def work(conversation: Conversation) -> str:
        stop = stop_signal_of(ctx)
        assert stop is not None
        await stop.guard(model(ctx.key))
        await conversation.turn.append_reply({"role": "assistant", "text": f"echo: {text}"})
        return f"echo: {text}"

    return await answer_once(ctx, text, ITEMS, work)


sleeper = AgentObject("sleeper", inactivity_timeout=timedelta(seconds=1))
sleeper.chat(answer, items=ITEMS)


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(restate.app([sleeper]), restate_image=RESTATE_IMAGE) as harness:
        yield harness


async def test_a_dead_attempts_model_request_is_cancelled(harness: HarnessEnvironment):
    # The attempt heartbeats every 20 s, by default: past the agent's inactivity timeout of a second, Restate
    # shuts each slow attempt down in the middle of its model request, and the next attempt asks again.
    key = str(uuid.uuid4())
    async with httpx2.AsyncClient(timeout=60) as ingress:
        sent = await ingress.post(
            f"{harness.ingress_url}/sleeper/{key}/chat/send", content="hello", headers={"content-type": "text/plain"}
        )
        invocation_id = typing.cast(str, sent.json()["invocationId"])
        try:
            answered = await ingress.get(f"{harness.ingress_url}/restate/invocation/{invocation_id}/attach", timeout=45)
        except httpx2.TimeoutException:
            answered = None
        finally:
            async with httpx2.AsyncClient() as admin:
                await admin.patch(f"{harness.admin_api_url}/invocations/{invocation_id}/kill")
    # Leave a dead attempt's request the time to end on its own, had it not been cancelled.
    await asyncio.sleep(SLOW + 1)
    counts = f"started {started[key]}, cancelled {cancelled[key]}, completed {completed[key]}"
    assert answered is not None, f"the turn did not answer in 45 s: {counts}"
    assert answered.status_code == 200, answered.text
    assert started[key] >= 3, counts
    assert completed[key] == 1, counts
    assert cancelled[key] == started[key] - 1, counts


async def test_cancelling_the_task_that_awaits_a_guard_cancels_its_work():
    # As the attempt's end cancels the handler's task, while the guard of its model request awaits.
    stop = Stop()
    events: list[str] = []

    async def work() -> None:
        events.append("started")
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            events.append("cancelled")
            raise
        events.append("completed")

    task = asyncio.ensure_future(stop.guard(work()))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.01)
    assert events == ["started", "cancelled"]
