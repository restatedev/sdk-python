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
"""
AI integration tests

Each test spins up a restate-server (via ``restate.create_test_harness``),
serves the `openai_service` app, and drives it with real OpenAI calls.

``disable_retries=True`` makes a journal mismatch / non-determinism fail the
invocation (and thus the test) instead of looping. ``always_replay=True`` forces
a suspend/replay on every await -- the HTTP/1.1-style path -- which is what
surfaces non-determinism bugs.
"""
import os
import asyncio
import restate
import pytest

import openai_service
from openai_service import message

# ----- Asyncio fixtures -----

@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def require_openai_key():
    """Fail (do not skip) the AI integration tests when no real OpenAI key is set.

    These tests are meaningless without a real model, so a missing key is treated
    as a hard failure rather than a silent skip.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.fail(
            "OPENAI_API_KEY is not set; the AI integration tests require a real OpenAI key.",
            pytrace=False,
        )


# ----- Tests -----

async def test_agent_tool_call_replays_cleanly():
    """A single tool-calling turn completes and replays without a journal mismatch."""
    async with restate.create_test_harness(
        openai_service.app(),
        disable_retries=True,
        always_replay=True,
    ) as h:
        out = await h.client.object_call(message, key="user-1", arg="What is the weather in Paris?")
        assert isinstance(out, str) and out.strip(), f"unexpected output: {out!r}"


async def test_multi_turn_session():
    """Session state persists across turns (and survives replay)."""
    async with restate.create_test_harness(
        openai_service.app(),
        disable_retries=True,
        always_replay=True,
    ) as h:
        await h.client.object_call(message, key="u", arg="My name is Ada. Remember it.")
        out = await h.client.object_call(message, key="u", arg="What is my name?")
        assert "ada" in out.lower(), f"session did not persist the name: {out!r}"


async def test_concurrent_distinct_keys():
    """Many invocations in parallel on distinct keys don't interfere."""
    async with restate.create_test_harness(
        openai_service.app(),
        disable_retries=True,
    ) as h:
        n = 20
        results = await asyncio.gather(
            *[h.client.object_call(message, key=f"user-{i}", arg="Reply with exactly: OK") for i in range(n)]
        )
        assert len(results) == n
        assert all(isinstance(r, str) and r.strip() for r in results)