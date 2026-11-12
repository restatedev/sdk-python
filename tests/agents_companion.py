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
"""Each agent's own companion, in process: named for its agent, keyed as its agent, and served with it only.

The companion's handlers are tested against a server in ``agents_signals.py``, and through an agent's views in
``agents_object.py``."""

# pylint: disable=missing-function-docstring,import-error

import pytest
from agents_conversation import ITEMS
from sessions_database import Backend
from sessions_object import Host, request

import restate
from restate import sessions
from restate.agents import AgentObject
from restate.agents._conversation import Conversation, answer_once
from restate.agents._signals import COMPANION_SUFFIX, companion_name, companion_of
from restate.endpoint import Endpoint
from restate.sessions.internal import invoke


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

REVISION = b'{"revision": 1}'


def agent(name: str) -> AgentObject:
    """An agent that echoes each message."""
    obj = AgentObject(name)

    @obj.chat(items=ITEMS)
    async def answer(ctx: sessions.SessionContext, text: str) -> str:
        async def run(conversation: Conversation) -> str:
            await conversation.turn.append_reply({"role": "assistant", "text": f"echo: {text}"})
            return f"echo: {text}"

        return await answer_once(ctx, text, ITEMS, run)

    return obj


def test_each_agent_has_a_companion_of_its_own_named_for_it():
    assert COMPANION_SUFFIX == ".agentprebuilt.restate"
    first, second = agent("first"), agent("second")
    assert companion_of(first).service.name == companion_name("first") == "first.agentprebuilt.restate"
    assert companion_of(second).service.name == "second.agentprebuilt.restate"
    assert companion_of(first).service is not companion_of(second).service
    assert (companion_of(first).agent, companion_of(second).agent) == ("first", "second")
    # The agent's name is taken as it is.
    assert companion_of(AgentObject("Odd-Name_1")).service.name == "Odd-Name_1.agentprebuilt.restate"


def test_an_app_serves_each_agent_with_its_companion_and_no_shared_signals():
    first, second = agent("first"), agent("second")
    endpoint = Endpoint().bind(first, second)
    assert list(endpoint.services) == [
        "first",
        "first.agentprebuilt.restate",
        "second",
        "second.agentprebuilt.restate",
    ]
    assert "signals" not in endpoint.services
    # The companion is ingress-private, all of it: clients reach it through its agent's views.
    for name in ("first.agentprebuilt.restate", "second.agentprebuilt.restate"):
        companion = endpoint.services[name]
        assert companion.ingress_private is True
        assert all(not handler.ingress_private for handler in companion.handlers.values())
    # Your own services may be named as you like, "signals" too.
    assert "signals" in Endpoint().bind(first, restate.VirtualObject("signals")).services


async def test_a_turn_calls_its_own_agents_companion_with_the_agents_key_and_not_the_others():
    first, second = agent("first"), agent("second")
    host = Host(Backend(), service="first", key="alice", answers={"start": REVISION, "close": b"null"})
    assert await invoke(first, "chat", host, request(), "hi") == "echo: hi"
    called = [(service, handler, key) for service, handler, _, key, _ in host.calls]
    assert called == [
        ("first.agentprebuilt.restate", "start", "alice"),
        ("first.agentprebuilt.restate", "close", "alice"),
    ]
    # Nothing reaches the other agent's companion.
    other = companion_of(second).service.name
    assert other not in [service for service, *_ in host.calls] + [send.service for send in host.sends]


async def test_the_views_ask_the_agents_own_companion_at_the_agents_key():
    first = agent("first")
    status = b'{"answering": null, "waiting": []}'
    view = Host(Backend(), service="first", key="bob", shared=True, answers={"status": status, "watch": REVISION})
    assert await invoke(first, "status", view, request(), None) == {"answering": None, "waiting": []}
    assert await invoke(first, "watch", view, request(), {"revision": 0}) == {"revision": 1}
    assert [(service, handler, key) for service, handler, _, key, _ in view.calls] == [
        ("first.agentprebuilt.restate", "status", "bob"),
        ("first.agentprebuilt.restate", "watch", "bob"),
    ]
