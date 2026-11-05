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
"""Restate's client, and the agent client on top of it, reach the key they are given, whatever its characters."""

# pylint: disable=missing-function-docstring

import json
import uuid
from datetime import timedelta

import pytest
from agents_conversation import ITEMS  # pylint: disable=import-error

import restate
from restate import HarnessEnvironment, sessions
from restate.agents import AgentObject
from restate.agents._conversation import Conversation, answer_once
from restate.agents.client import AgentClient
from restate.client import create_client


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

echo = restate.VirtualObject("echo")


@echo.handler()
async def whose(ctx: restate.ObjectContext, text: str) -> str:
    return f"{ctx.key()}: {text}"


agent = AgentObject("keyed")


async def answer(ctx: sessions.SessionContext, text: str) -> str:
    async def run(conversation: Conversation) -> str:
        await conversation.append_reply({"role": "assistant", "text": ctx.key})
        return ctx.key

    return await answer_once(ctx, text, ITEMS, run)


agent.chat(answer, items=ITEMS)


@pytest.fixture(scope="module")
async def harness():
    async with restate.create_test_harness(
        sessions.app(echo, agent, slow), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


JSON = {"Content-Type": "application/json"}

KEYS = ["plain", "a/b", "a b", "a?b=c", "a#b", "100%", "ünï/cødé", "a/../b", ".", ".."]
"""Keys whose characters a URL path gives a meaning of its own, unless they are encoded."""


@pytest.mark.parametrize("key", KEYS)
async def test_a_call_reaches_the_key_it_names(harness: HarnessEnvironment, key: str):
    async with create_client(harness.ingress_url) as client:
        answered = await client.generic_call("echo", "whose", json.dumps("hi").encode(), key=key, headers=JSON)
        assert json.loads(answered) == f"{key}: hi"
        sent = await client.generic_send("echo", "whose", json.dumps("there").encode(), key=key, headers=JSON)
        assert sent.invocation_id


@pytest.mark.parametrize("key", KEYS)
async def test_the_agent_client_reaches_the_key_it_names(harness: HarnessEnvironment, key: str):
    unique = f"{key}-{uuid.uuid4().hex[:6]}"
    async with create_client(harness.ingress_url) as client:
        keyed = AgentClient(client, "keyed", unique)
        assert await keyed.chat("Hi") == unique
        assert [m["parts"][0].get("text") for m in (await keyed.messages())["messages"]] == ["Hi", unique]
        assert (await keyed.status())["answering"] is None


slow = restate.VirtualObject("slow")


@slow.handler()
async def answer_slowly(ctx: restate.ObjectContext, seconds: float) -> str:
    await ctx.sleep(timedelta(seconds=seconds))
    return "done"


async def test_a_call_waits_as_long_as_its_handler_takes(harness: HarnessEnvironment):
    # An agent's answer takes as long as its model: longer than an HTTP client's default timeout (5 seconds).
    async with create_client(harness.ingress_url) as client:
        answered = await client.generic_call("slow", "answer_slowly", b"7", key="k", headers=JSON)
        assert json.loads(answered) == "done"
