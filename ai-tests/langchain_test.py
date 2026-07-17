"""LangChain integration tests against scripted and live OpenAI models."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import restate
from langchain_openai import ChatOpenAI
from restate import HarnessEnvironment
from restate.client_types import HttpError

import langchain_service
from langchain_model_stub import scripted_agenerate
from langchain_service import (
    coordinator_run,
    failing_run,
    get_session_messages,
    message,
    triage_run,
)

AgentMessages = list[dict[str, Any]]


def response_text(messages: AgentMessages) -> str:
    text = ""
    for serialized_message in messages:
        if serialized_message.get("type") != "ai" or not isinstance(data := serialized_message.get("data"), dict):
            continue
        content = data.get("content")
        if isinstance(content, str):
            text += content
        elif isinstance(content, list):
            text += "".join(
                str(block.get("text", "")) for block in content if isinstance(block, dict) and block.get("text")
            )
    return text


def assert_completed(messages: AgentMessages) -> None:
    assert response_text(messages).strip(), f"run did not produce a final text response: {messages!r}"


def assert_tool_executed(messages: AgentMessages, tool_name: str, minimum_calls: int = 1) -> None:
    call_ids = [
        tool_call.get("id")
        for serialized_message in messages
        if serialized_message.get("type") == "ai" and isinstance(data := serialized_message.get("data"), dict)
        for tool_call in data.get("tool_calls", [])
        if isinstance(tool_call, dict) and tool_call.get("name") == tool_name
    ]
    assert len(call_ids) >= minimum_calls, f"expected {tool_name!r} to be called, got: {messages!r}"

    output_ids = {
        data.get("tool_call_id")
        for serialized_message in messages
        if serialized_message.get("type") == "tool" and isinstance(data := serialized_message.get("data"), dict)
    }
    missing_outputs = [call_id for call_id in call_ids if call_id not in output_ids]
    assert not missing_outputs, f"{tool_name!r} calls did not complete: {missing_outputs!r}"


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture
def scripted_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ChatOpenAI, "_agenerate", scripted_agenerate)


@pytest.fixture
def live_model() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.fail(
            "OPENAI_API_KEY is not set; live LangChain tests require a real OpenAI key.",
            pytrace=False,
        )


@pytest.fixture(scope="session")
async def restate_test_harness() -> AsyncIterator[HarnessEnvironment]:
    async with restate.create_test_harness(
        langchain_service.app(),
        follow_logs=True,
        disable_retries=True,
        always_replay=True,
    ) as harness:
        yield harness


async def test_agent_tool_call_replays_cleanly(
    restate_test_harness: HarnessEnvironment,
    scripted_model: None,
):
    messages = await restate_test_harness.client.object_call(
        message,
        key="scripted-weather",
        arg="What is the weather in Paris?",
    )
    assert_tool_executed(messages, "get_weather")
    assert_completed(messages)


@pytest.mark.live_model
async def test_agent_tool_call_live_replays_cleanly(
    restate_test_harness: HarnessEnvironment,
    live_model: None,
):
    messages = await restate_test_harness.client.object_call(
        message,
        key="live-weather",
        arg="What is the weather in Paris?",
    )
    assert_tool_executed(messages, "get_weather")
    assert_completed(messages)


async def test_multi_turn_session(
    restate_test_harness: HarnessEnvironment,
    scripted_model: None,
):
    first_messages = await restate_test_harness.client.object_call(
        message,
        key="scripted-multi-turn",
        arg="What is the capital of France?",
    )
    second_messages = await restate_test_harness.client.object_call(
        message,
        key="scripted-multi-turn",
        arg="In which country is it?",
    )
    session_messages = await restate_test_harness.client.object_call(
        get_session_messages,
        key="scripted-multi-turn",
        arg=None,
    )
    assert_completed(first_messages)
    assert_completed(second_messages)
    assert "france" in response_text(second_messages).lower()
    assert session_messages, "LangChain session state is empty"


@pytest.mark.live_model
async def test_multi_turn_session_live(
    restate_test_harness: HarnessEnvironment,
    live_model: None,
):
    first_messages = await restate_test_harness.client.object_call(
        message,
        key="live-multi-turn",
        arg="What is the capital of France?",
    )
    second_messages = await restate_test_harness.client.object_call(
        message,
        key="live-multi-turn",
        arg="In which country is it?",
    )
    session_messages = await restate_test_harness.client.object_call(
        get_session_messages,
        key="live-multi-turn",
        arg=None,
    )
    assert_completed(first_messages)
    assert_completed(second_messages)
    assert "france" in response_text(second_messages).lower()
    assert session_messages, "LangChain session state is empty"


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
