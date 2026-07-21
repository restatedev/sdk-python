#
#  Copyright (c) 2023-2025 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""OpenAI Agents integration tests.

All scenarios run against a scripted model (no key needed) with always-replay;
the tool-call and multi-turn scenarios additionally run against the live OpenAI
API to catch real provider response-type drift through the journaling path.
"""

from __future__ import annotations

import asyncio
import os
import pytest
import restate
import openai_service
from typing import Any
from collections.abc import AsyncIterator
from agents.models.multi_provider import MultiProvider
from restate import HarnessEnvironment
from restate.client_types import HttpError
from openai_service import (
    coordinator_run,
    failing_run,
    get_session_items,
    message,
    triage_run,
)
from openai_model_stub import ScriptedModel


AgentRunItems = list[dict[str, Any]]


def assert_completed(items: AgentRunItems) -> None:
    assert any(item.get("type") == "message" for item in items), f"run did not produce a final message: {items!r}"


def response_text(items: AgentRunItems) -> str:
    return "".join(
        str(content.get("text", ""))
        for item in items
        if item.get("type") == "message"
        for content in item.get("content", [])
        if isinstance(content, dict) and content.get("type") == "output_text"
    )


def assert_tool_executed(items: AgentRunItems, tool_name: str, minimum_calls: int = 1) -> None:
    call_ids = [
        item.get("call_id") for item in items if item.get("type") == "function_call" and item.get("name") == tool_name
    ]
    assert len(call_ids) >= minimum_calls, f"expected {tool_name!r} to be called, got: {items!r}"

    output_ids = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    missing_outputs = [call_id for call_id in call_ids if call_id not in output_ids]
    assert not missing_outputs, f"{tool_name!r} calls did not complete: {missing_outputs!r}"


def assert_handoff_executed(items: AgentRunItems) -> None:
    handoff_names = {
        item.get("name")
        for item in items
        if item.get("type") == "function_call" and str(item.get("name", "")).startswith("transfer_to_")
    }
    assert handoff_names, f"expected an agent handoff, got: {items!r}"
    for handoff_name in handoff_names:
        assert_tool_executed(items, str(handoff_name))


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


def _use_scripted_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MultiProvider, "get_model", lambda _self, _model_name: ScriptedModel())


@pytest.fixture
def scripted_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route all model calls to the ScriptedModel (no OpenAI key needed)."""
    _use_scripted_model(monkeypatch)


# Scripted mode covers every scenario; live mode runs only where it adds unique
# value -- validating the real provider's response types survive journaling.
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
                "OPENAI_API_KEY is not set; live AI integration tests require a real OpenAI key.", pytrace=False
            )
    else:
        _use_scripted_model(monkeypatch)
    return mode


@pytest.fixture(scope="session")
async def restate_test_harness() -> AsyncIterator[HarnessEnvironment]:
    async with restate.create_test_harness(
        openai_service.app(),
        follow_logs=True,
        disable_retries=True,
        always_replay=True,
    ) as harness:
        yield harness


async def test_agent_tool_call_replays_cleanly(
    restate_test_harness: HarnessEnvironment,
    model_mode: str,
):
    items = await restate_test_harness.client.object_call(
        message,
        key=f"{model_mode}-weather",
        arg="What is the weather in Paris?",
    )
    assert_tool_executed(items, "get_weather")
    assert_completed(items)


async def test_multi_turn_session(
    restate_test_harness: HarnessEnvironment,
    model_mode: str,
):
    key = f"{model_mode}-multi-turn"
    first_items = await restate_test_harness.client.object_call(
        message,
        key=key,
        arg="What is the capital of France?",
    )
    second_items = await restate_test_harness.client.object_call(
        message,
        key=key,
        arg="In which country is it?",
    )
    session_items = await restate_test_harness.client.object_call(get_session_items, key=key, arg=None)
    assert_completed(first_items)
    assert_completed(second_items)

    # Context of first question, was available to the second question.
    assert "france" in response_text(second_items).lower()

    # Session state is stored in Restate.
    assert session_items, "OpenAI Agents session state is empty"


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
    for items in results:
        assert_completed(items)


async def test_parallel_tools_turnstile(
    restate_test_harness: HarnessEnvironment,
    scripted_model: None,
):
    cities = ["Paris", "London", "Tokyo", "Berlin", "Rome"]
    items = await restate_test_harness.client.object_call(
        message,
        key="parallel-weather",
        arg=f"What is the weather in {', '.join(cities)}? Give one line per city.",
    )
    assert_tool_executed(items, "get_weather", minimum_calls=len(cities))
    assert_completed(items)


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
    items = await restate_test_harness.client.service_call(
        triage_run,
        arg="I was double charged on my invoice, can I get a refund?",
    )
    assert_handoff_executed(items)
    assert_completed(items)


async def test_remote_handoff_serializes_across_rpc(
    restate_test_harness: HarnessEnvironment,
    scripted_model: None,
):
    items = await restate_test_harness.client.service_call(
        coordinator_run,
        arg="How do I speed up a slow SQL query with a missing index?",
    )
    assert_tool_executed(items, "ask_specialist")
    assert_completed(items)
