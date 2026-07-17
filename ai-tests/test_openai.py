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
import uuid
import pytest
import restate
import openai_service
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from agents.models.multi_provider import MultiProvider
from restate import ObjectContext
from restate.client_types import HttpError, RestateClient
from openai_service import (
    coordinator_run,
    failing_run,
    message,
    triage_run,
)
from model_stub import ScriptedModel


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


@asynccontextmanager
async def harness_client() -> AsyncIterator[RestateClient]:
    async with restate.create_test_harness(
        openai_service.app(),
        follow_logs=True,
        disable_retries=True,
        always_replay=True,
    ) as harness:
        yield harness.client


async def test_agent_tool_call_replays_cleanly(model_mode: str):
    async with harness_client() as client:
        out = await client.object_call(message, key="user-1", arg="What is the weather in Paris?")
        assert isinstance(out, str) and out.strip(), f"unexpected output: {out!r}"


async def test_multi_turn_session(model_mode: str):
    async with harness_client() as client:
        await client.object_call(message, key="u", arg="My name is Ada. Remember it.")
        out = await client.object_call(message, key="u", arg="What is my name?")
        assert isinstance(out, str) and out.strip(), f"unexpected output: {out!r}"


async def test_concurrent_distinct_keys(scripted_model: None):
    async with harness_client() as client:
        n = 20
        results = await asyncio.gather(
            *[client.object_call(message, key=f"user-{i}", arg="Reply with exactly: OK") for i in range(n)]
        )
        assert len(results) == n
        assert all(isinstance(result, str) and result.strip() for result in results)


async def test_parallel_tools_turnstile(scripted_model: None):
    cities = ["Paris", "London", "Tokyo", "Berlin", "Rome"]
    async with harness_client() as client:
        out = await client.object_call(
            message,
            key="cities",
            arg=f"What is the weather in {', '.join(cities)}? Give one line per city.",
        )
        assert isinstance(out, str) and out.strip(), f"unexpected output: {out!r}"


async def test_terminal_tool_error_fails_fast(scripted_model: None):
    async with harness_client() as client:
        with pytest.raises(HttpError) as exc:
            await client.service_call(failing_run, arg="please process my request")
        assert exc.value.status_code == 500, f"unexpected status: {exc.value.status_code}"


async def test_local_handoff(scripted_model: None):
    async with harness_client() as client:
        out = await client.service_call(triage_run, arg="I was double charged on my invoice, can I get a refund?")
        assert isinstance(out, str) and out.strip(), f"unexpected output: {out!r}"


async def test_remote_handoff_serializes_across_rpc(scripted_model: None):
    async with harness_client() as client:
        out = await client.service_call(coordinator_run, arg="How do I speed up a slow SQL query with a missing index?")
        assert isinstance(out, str) and out.strip(), f"unexpected output: {out!r}"
