"""Google ADK integration tests against scripted and live Gemini models."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
import restate
from google.adk.models.google_llm import Gemini
from restate import HarnessEnvironment
from restate.client_types import HttpError

import google_adk_service
from google_adk_model_stub import scripted_generate_content_async
from google_adk_service import (
    coordinator_run,
    failing_run,
    get_session_events,
    message,
    triage_run,
)

AgentEvents = list[dict[str, Any]]


def print_events(label: str, events: object) -> None:
    print(f"\n--- {label}: new events ---", flush=True)
    print(json.dumps(events, indent=2, sort_keys=True, default=str), flush=True)


def event_parts(events: AgentEvents) -> list[dict[str, Any]]:
    return [
        part
        for event in events
        if isinstance(event.get("content"), dict)
        for part in event["content"].get("parts", [])
        if isinstance(part, dict)
    ]


def response_text(events: AgentEvents) -> str:
    return "".join(str(part.get("text", "")) for part in event_parts(events) if part.get("text"))


def assert_completed(events: AgentEvents) -> None:
    assert response_text(events).strip(), f"run did not produce a final text response: {events!r}"


def assert_tool_executed(events: AgentEvents, tool_name: str, minimum_calls: int = 1) -> None:
    parts = event_parts(events)
    call_ids = [
        function_call.get("id")
        for part in parts
        if isinstance(function_call := part.get("function_call"), dict) and function_call.get("name") == tool_name
    ]
    assert len(call_ids) >= minimum_calls, f"expected {tool_name!r} to be called, got: {events!r}"

    response_ids = {
        function_response.get("id")
        for part in parts
        if isinstance(function_response := part.get("function_response"), dict)
    }
    missing_responses = [call_id for call_id in call_ids if call_id not in response_ids]
    assert not missing_responses, f"{tool_name!r} calls did not complete: {missing_responses!r}"


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture
def scripted_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Gemini, "generate_content_async", scripted_generate_content_async)


@pytest.fixture
def live_model() -> None:
    if not os.environ.get("GOOGLE_API_KEY"):
        pytest.fail(
            "GOOGLE_API_KEY is not set; live Google ADK tests require a real Gemini key.",
            pytrace=False,
        )


@pytest.fixture(scope="session")
async def restate_test_harness() -> AsyncIterator[HarnessEnvironment]:
    async with restate.create_test_harness(
        google_adk_service.app(),
        follow_logs=True,
        disable_retries=True,
        always_replay=True,
    ) as harness:
        yield harness


async def test_agent_tool_call_replays_cleanly(
    restate_test_harness: HarnessEnvironment,
    scripted_model: None,
):
    events = await restate_test_harness.client.object_call(
        message,
        key="scripted-weather",
        arg="What is the weather in Paris?",
    )
    print_events("weather tool (scripted)", events)
    assert_tool_executed(events, "get_weather")
    assert_completed(events)


@pytest.mark.live_model
async def test_agent_tool_call_live_replays_cleanly(
    restate_test_harness: HarnessEnvironment,
    live_model: None,
):
    events = await restate_test_harness.client.object_call(
        message,
        key="live-weather",
        arg="What is the weather in Paris?",
    )
    print_events("weather tool (live)", events)
    assert_tool_executed(events, "get_weather")
    assert_completed(events)


async def test_multi_turn_session(
    restate_test_harness: HarnessEnvironment,
    scripted_model: None,
):
    first_events = await restate_test_harness.client.object_call(
        message,
        key="scripted-multi-turn",
        arg="What is the capital of France?",
    )
    second_events = await restate_test_harness.client.object_call(
        message,
        key="scripted-multi-turn",
        arg="In which country is it?",
    )
    session_events = await restate_test_harness.client.object_call(
        get_session_events,
        key="scripted-multi-turn",
        arg=None,
    )
    print_events("multi-turn first message (scripted)", first_events)
    print_events("multi-turn second message (scripted)", second_events)
    print_events("multi-turn session state (scripted)", session_events)
    assert_completed(first_events)
    assert_completed(second_events)
    assert "france" in response_text(second_events).lower()
    assert session_events, "Google ADK session state is empty"


@pytest.mark.live_model
async def test_multi_turn_session_live(
    restate_test_harness: HarnessEnvironment,
    live_model: None,
):
    first_events = await restate_test_harness.client.object_call(
        message,
        key="live-multi-turn",
        arg="What is the capital of France?",
    )
    second_events = await restate_test_harness.client.object_call(
        message,
        key="live-multi-turn",
        arg="In which country is it?",
    )
    session_events = await restate_test_harness.client.object_call(
        get_session_events,
        key="live-multi-turn",
        arg=None,
    )
    print_events("multi-turn first message (live)", first_events)
    print_events("multi-turn second message (live)", second_events)
    print_events("multi-turn session state (live)", session_events)
    assert_completed(first_events)
    assert_completed(second_events)
    assert "france" in response_text(second_events).lower()
    assert session_events, "Google ADK session state is empty"


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
    print_events("concurrent distinct keys", results)
    assert len(results) == count
    for events in results:
        assert_completed(events)


async def test_parallel_tools_turnstile(
    restate_test_harness: HarnessEnvironment,
    scripted_model: None,
):
    cities = ["Paris", "London", "Tokyo", "Berlin", "Rome"]
    events = await restate_test_harness.client.object_call(
        message,
        key="parallel-weather",
        arg=f"What is the weather in {', '.join(cities)}? Give one line per city.",
    )
    print_events("parallel weather tools", events)
    assert_tool_executed(events, "get_weather", minimum_calls=len(cities))
    assert_completed(events)


async def test_terminal_tool_error_fails_fast(
    restate_test_harness: HarnessEnvironment,
    scripted_model: None,
):
    with pytest.raises(HttpError) as exc:
        await restate_test_harness.client.object_call(
            failing_run,
            key="terminal-error",
            arg="please process my request",
        )
    print_events("terminal tool error", {"status_code": exc.value.status_code, "body": exc.value.body})
    assert exc.value.status_code == 500, f"unexpected status: {exc.value.status_code}"
    assert "tool failed permanently" in (exc.value.body or "")


async def test_local_handoff(
    restate_test_harness: HarnessEnvironment,
    scripted_model: None,
):
    events = await restate_test_harness.client.object_call(
        triage_run,
        key="local-handoff",
        arg="I was double charged on my invoice, can I get a refund?",
    )
    print_events("local handoff", events)
    assert_tool_executed(events, "transfer_to_agent")
    assert_completed(events)


async def test_remote_handoff_serializes_across_rpc(
    restate_test_harness: HarnessEnvironment,
    scripted_model: None,
):
    events = await restate_test_harness.client.service_call(
        coordinator_run,
        arg="How do I speed up a slow SQL query with a missing index?",
    )
    print_events("remote specialist tool", events)
    assert_tool_executed(events, "ask_specialist")
    assert_completed(events)
