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
"""Unit test for the StateGraph determinism coordinator.

The coordinator is what makes parallel LangGraph nodes journal deterministically:
given a set of concurrently-arriving durable ops (one per parallel task), it must
create their journal commands in a STABLE, sorted-by-key order regardless of the
order they arrived or completed. This test drives the coordinator directly (no
server, no langgraph needed) and asserts that property.
"""

import asyncio

import pytest

from restate.ext.langchain._stategraph import _Coordinator

pytestmark = [pytest.mark.anyio]


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


async def test_coordinator_creates_commands_in_sorted_key_order():
    coord = _Coordinator(settle_turns=0)
    creation_order: list[str] = []

    # Hierarchical, replay-stable keys, deliberately submitted out of order.
    keys = ["/w|0003", "/w|0001", "/w|0004", "/w|0002", "/w|0000"]

    # Eager registration (mirrors the runner hook tagging every superstep task at
    # start), so the coordinator waits for the whole batch before flushing.
    for k in keys:
        coord.register(k)

    async def leaf(key: str):
        def make_future():
            # make_future() is called at flush time; record the CREATION order
            # (this is the order commands would be appended to the journal).
            creation_order.append(key)

            async def io():
                await asyncio.sleep(0)  # simulate concurrent I/O completion
                return f"result:{key}"

            return io()

        try:
            return await coord.submit(key, make_future)
        finally:
            coord.checkout(key)

    results = await asyncio.gather(*(leaf(k) for k in keys))

    # Commands were created in sorted key order — independent of arrival order.
    assert creation_order == sorted(keys)
    # Every op still got its own result routed back.
    assert set(results) == {f"result:{k}" for k in keys}


async def test_coordinator_orders_concurrent_tool_call_tasks():
    # A node's concurrent tool calls run as SEPARATE Pregel push tasks with
    # distinct hierarchical keys (verified against LangGraph: a tool-using node
    # "deep" spawns .../tools|('__pregel_push', N, False) tasks). The coordinator
    # must order those distinct-key ops deterministically and never collide,
    # alongside sibling single-op nodes — all in one flush round.
    coord = _Coordinator(settle_turns=0)
    order: list[str] = []
    tool_tasks = [f"/deep|(pull)/tools|(push,{i})" for i in range(3)]
    sibling_tasks = ["/branch_b|(pull)", "/branch_a|(pull)"]
    all_keys = tool_tasks + sibling_tasks
    for k in all_keys:
        coord.register(k)

    async def leaf(key: str):
        def make_future():
            order.append(key)

            async def io():
                await asyncio.sleep(0)
                return key

            return io()

        try:
            return await coord.submit(key, make_future)
        finally:
            coord.checkout(key)

    results = await asyncio.gather(*(leaf(k) for k in all_keys))

    # All ops created (none lost to a same-key collision), in sorted key order.
    assert len(order) == len(all_keys)
    assert order == sorted(all_keys)
    assert set(results) == set(all_keys)


async def test_coordinator_multiple_rounds_stay_ordered():
    coord = _Coordinator(settle_turns=0)
    rounds: list[list[str]] = [[], []]
    keys = ["/w|0002", "/w|0000", "/w|0001"]
    for k in keys:
        coord.register(k)

    async def leaf(key: str):
        for r in (0, 1):  # two sequential durable ops per task

            def make_future(_r=r, _k=key):
                rounds[_r].append(_k)

                async def io():
                    await asyncio.sleep(0)
                    return _k

                return io()

            await coord.submit(key, make_future)
        coord.checkout(key)

    await asyncio.gather(*(leaf(k) for k in keys))

    # Each round's commands are independently sorted.
    assert rounds[0] == sorted(keys)
    assert rounds[1] == sorted(keys)
