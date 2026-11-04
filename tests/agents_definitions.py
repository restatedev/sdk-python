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
"""Defining session objects and agents: what is refused at definition time, and how, before anything runs."""

# pylint: disable=missing-function-docstring

import pytest

from restate import sessions
from restate.agents import AgentObject
from restate.agents.internal import provider_of


def test_migrations_given_as_one_string_are_refused():
    with pytest.raises(TypeError, match="a list of steps, not a string"):
        sessions.SessionObject("p", migrations="CREATE TABLE x (a)")  # type: ignore[arg-type]


def test_a_sync_function_is_refused_as_a_handler_or_view():
    obj = sessions.SessionObject("sync")

    def handler(ctx: sessions.SessionContext) -> None: ...

    with pytest.raises(TypeError, match="must be a coroutine function"):
        obj.handler(handler)  # type: ignore[type-var]
    with pytest.raises(TypeError, match="must be a coroutine function"):
        obj.view(handler)  # type: ignore[type-var]


def test_a_second_handler_of_a_name_is_refused_with_the_name():
    obj = sessions.SessionObject("twice")

    @obj.handler
    async def add(ctx: sessions.SessionContext) -> None: ...

    async def listing(ctx: sessions.ViewContext) -> None: ...

    with pytest.raises(ValueError, match="'add' already"):
        obj.view(name="add")(listing)


def test_an_agents_built_in_names_are_refused_with_what_to_do():
    agent = AgentObject("helper")

    async def status(ctx: sessions.SessionContext) -> str:
        return "mine"

    with pytest.raises(ValueError, match=r"'status' is a built-in handler of every agent.*name='my_status'"):
        agent.handler(status)


def test_chat_is_a_decorator_too_and_returns_the_function():
    agent = AgentObject("decorated")

    @agent.chat
    async def answer(ctx: sessions.SessionContext, text: str) -> str:
        return text

    assert answer.__name__ == "answer"
    assert "chat" in agent.handlers and "messages" in agent.handlers

    other = AgentObject("decorated_with_clear")

    async def forget(ctx: sessions.SessionContext) -> None: ...

    @other.chat(clear=forget)
    async def reply(ctx: sessions.SessionContext, text: str) -> str:
        return text

    assert reply.__name__ == "reply" and "chat" in other.handlers


def test_objects_have_a_readable_repr():
    assert repr(sessions.SessionObject("notes")) == "SessionObject('notes')"
    assert repr(AgentObject("assistant")) == "AgentObject('assistant')"


def test_a_model_names_its_provider_with_a_slash_or_a_colon():
    assert provider_of("openai/gpt-6-sol") == ("openai", "gpt-6-sol")
    assert provider_of("anthropic:claude-sonnet-5-5") == ("anthropic", "claude-sonnet-5-5")
    assert provider_of("openai/ft:gpt-4o:acme") == ("openai", "ft:gpt-4o:acme")
    assert provider_of("gpt") == ("", "gpt")
