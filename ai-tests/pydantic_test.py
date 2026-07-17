"""Pydantic AI integration tests against scripted and live models."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import restate
from restate.client_types import HttpError, RestateClient
from restate.ext.pydantic import RestateModelWrapper

import pydantic_service
from pydantic_model_stub import ScriptedPydanticModel
from pydantic_service import (
    coordinator_run,
    failing_run,
    get_session_messages,
    message,
    triage_run,
)

AgentMessages = list[dict[str, Any]]


def print_messages(label: str, messages: object) -> None:
    print(f"\n--- {label}: new messages ---", flush=True)
    print(json.dumps(messages, indent=2, sort_keys=True, default=str), flush=True)


def message_parts(messages: AgentMessages) -> list[dict[str, Any]]:
    return [part for message in messages for part in message.get("parts", []) if isinstance(part, dict)]


def response_text(messages: AgentMessages) -> str:
    return "".join(str(part.get("content", "")) for part in message_parts(messages) if part.get("part_kind") == "text")


def assert_completed(messages: AgentMessages) -> None:
    assert response_text(messages).strip(), f"run did not produce a final text response: {messages!r}"


def assert_tool_executed(messages: AgentMessages, tool_name: str, minimum_calls: int = 1) -> None:
    parts = message_parts(messages)
    call_ids = [
        part.get("tool_call_id")
        for part in parts
        if part.get("part_kind") == "tool-call" and part.get("tool_name") == tool_name
    ]
    assert len(call_ids) >= minimum_calls, f"expected {tool_name!r} to be called, got: {messages!r}"

    return_ids = {part.get("tool_call_id") for part in parts if part.get("part_kind") == "tool-return"}
    missing_returns = [call_id for call_id in call_ids if call_id not in return_ids]
    assert not missing_returns, f"{tool_name!r} calls did not complete: {missing_returns!r}"


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


def use_scripted_model(monkeypatch: pytest.MonkeyPatch) -> None:
    model = ScriptedPydanticModel()
    for agent in pydantic_service.DURABLE_AGENTS:
        wrapper = agent.model
        assert isinstance(wrapper, RestateModelWrapper)
        monkeypatch.setattr(wrapper, "wrapped", model)


@pytest.fixture
def scripted_model(monkeypatch: pytest.MonkeyPatch) -> None:
    use_scripted_model(monkeypatch)


@pytest.fixture(
    params=[
        pytest.param("scripted", id="scripted"),
        pytest.param("live", id="live", marks=pytest.mark.live_model),
    ]
)
def model_mode(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    mode = request.param
    if mode == "live":
        if not os.environ.get("OPENAI_API_KEY"):
            pytest.fail(
                "OPENAI_API_KEY is not set; live AI integration tests require a real OpenAI key.",
                pytrace=False,
            )
    else:
        use_scripted_model(monkeypatch)
    return mode


@asynccontextmanager
async def harness_client() -> AsyncIterator[RestateClient]:
    async with restate.create_test_harness(
        pydantic_service.app(),
        follow_logs=True,
        disable_retries=True,
        always_replay=True,
    ) as harness:
        yield harness.client


async def test_agent_tool_call_replays_cleanly(model_mode: str):
    async with harness_client() as client:
        messages = await client.object_call(message, key="user-1", arg="What is the weather in Paris?")
        print_messages(f"weather tool ({model_mode})", messages)
        assert_tool_executed(messages, "get_weather")
        assert_completed(messages)


async def test_multi_turn_session(model_mode: str):
    async with harness_client() as client:
        first_messages = await client.object_call(message, key="u", arg="What is the capital of France?")
        second_messages = await client.object_call(message, key="u", arg="In which country is it?")
        session_messages = await client.object_call(get_session_messages, key="u", arg=None)
        print_messages(f"multi-turn first message ({model_mode})", first_messages)
        print_messages(f"multi-turn second message ({model_mode})", second_messages)
        print_messages(f"multi-turn session state ({model_mode})", session_messages)
        assert_completed(first_messages)
        assert_completed(second_messages)
        assert "paris" in response_text(first_messages).lower()
        assert session_messages, "Pydantic AI session state is empty"


async def test_concurrent_distinct_keys(scripted_model: None):
    async with harness_client() as client:
        count = 20
        results = await asyncio.gather(
            *[client.object_call(message, key=f"user-{index}", arg="Reply with exactly: OK") for index in range(count)]
        )
        print_messages("concurrent distinct keys", results)
        assert len(results) == count
        for messages in results:
            assert_completed(messages)


async def test_parallel_tools_turnstile(scripted_model: None):
    cities = ["Paris", "London", "Tokyo", "Berlin", "Rome"]
    async with harness_client() as client:
        messages = await client.object_call(
            message,
            key="cities",
            arg=f"What is the weather in {', '.join(cities)}? Give one line per city.",
        )
        print_messages("parallel weather tools", messages)
        assert_tool_executed(messages, "get_weather", minimum_calls=len(cities))
        assert_completed(messages)


async def test_terminal_tool_error_fails_fast(scripted_model: None):
    async with harness_client() as client:
        with pytest.raises(HttpError) as exc:
            await client.service_call(failing_run, arg="please process my request")
        print_messages("terminal tool error", {"status_code": exc.value.status_code, "body": exc.value.body})
        assert exc.value.status_code == 500, f"unexpected status: {exc.value.status_code}"
        assert "tool failed permanently" in (exc.value.body or "")


async def test_local_handoff(scripted_model: None):
    async with harness_client() as client:
        messages = await client.service_call(triage_run, arg="I was double charged on my invoice, can I get a refund?")
        print_messages("local handoff", messages)
        assert_tool_executed(messages, "handoff_to_billing")
        assert_completed(messages)


async def test_remote_handoff_serializes_across_rpc(scripted_model: None):
    async with harness_client() as client:
        messages = await client.service_call(
            coordinator_run, arg="How do I speed up a slow SQL query with a missing index?"
        )
        print_messages("remote specialist tool", messages)
        assert_tool_executed(messages, "ask_specialist")
        assert_completed(messages)
