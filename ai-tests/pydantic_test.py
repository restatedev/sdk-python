"""Pydantic AI integration tests against scripted and live models."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import restate
from restate import HarnessEnvironment
from restate.client_types import HttpError
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


@pytest.fixture(scope="session")
async def restate_test_harness() -> AsyncIterator[HarnessEnvironment]:
    async with restate.create_test_harness(
        pydantic_service.app(),
        follow_logs=True,
        disable_retries=True,
        always_replay=True,
    ) as harness:
        yield harness


async def test_agent_tool_call_replays_cleanly(
    restate_test_harness: HarnessEnvironment,
    model_mode: str,
):
    messages = await restate_test_harness.client.object_call(
        message,
        key=f"{model_mode}-weather",
        arg="What is the weather in Paris?",
    )
    assert_tool_executed(messages, "get_weather")
    assert_completed(messages)


async def test_multi_turn_session(
    restate_test_harness: HarnessEnvironment,
    model_mode: str,
):
    key = f"{model_mode}-multi-turn"
    first_messages = await restate_test_harness.client.object_call(
        message,
        key=key,
        arg="What is the capital of France?",
    )
    second_messages = await restate_test_harness.client.object_call(
        message,
        key=key,
        arg="In which country is it?",
    )
    session_messages = await restate_test_harness.client.object_call(get_session_messages, key=key, arg=None)
    assert_completed(first_messages)
    assert_completed(second_messages)
    assert "paris" in response_text(first_messages).lower()
    assert session_messages, "Pydantic AI session state is empty"


async def test_concurrent_distinct_keys(
    restate_test_harness: HarnessEnvironment,
    scripted_model: None,
):
    count = 20
    results = await asyncio.gather(
        *[
            restate_test_harness.client.object_call(
                message,
                key=f"concurrent-user-{index}",
                arg="Reply with exactly: OK",
            )
            for index in range(count)
        ]
    )
    assert len(results) == count
    for messages in results:
        assert_completed(messages)


async def test_parallel_tools_turnstile(
    restate_test_harness: HarnessEnvironment,
    scripted_model: None,
):
    cities = ["Paris", "London", "Tokyo", "Berlin", "Rome"]
    messages = await restate_test_harness.client.object_call(
        message,
        key="parallel-weather",
        arg=f"What is the weather in {', '.join(cities)}? Give one line per city.",
    )
    assert_tool_executed(messages, "get_weather", minimum_calls=len(cities))
    assert_completed(messages)


async def test_terminal_tool_error_fails_fast(
    restate_test_harness: HarnessEnvironment,
    scripted_model: None,
):
    with pytest.raises(HttpError) as exc:
        await restate_test_harness.client.service_call(failing_run, arg="please process my request")
    assert exc.value.status_code == 500, f"unexpected status: {exc.value.status_code}"
    assert "tool failed permanently" in (exc.value.body or "")


async def test_local_handoff(
    restate_test_harness: HarnessEnvironment,
    scripted_model: None,
):
    messages = await restate_test_harness.client.service_call(
        triage_run,
        arg="I was double charged on my invoice, can I get a refund?",
    )
    assert_tool_executed(messages, "handoff_to_billing")
    assert_completed(messages)


async def test_remote_handoff_serializes_across_rpc(
    restate_test_harness: HarnessEnvironment,
    scripted_model: None,
):
    messages = await restate_test_harness.client.service_call(
        coordinator_run,
        arg="How do I speed up a slow SQL query with a missing index?",
    )
    assert_tool_executed(messages, "ask_specialist")
    assert_completed(messages)
