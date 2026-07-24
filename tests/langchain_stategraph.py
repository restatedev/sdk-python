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
"""Determinism test for `restate.ext.langchain.durable_scope` over a StateGraph.

Proves the core property without a server: parallel nodes (here, a fan-out to
subgraphs that themselves fan out) create their durable commands in a stable,
sorted order every run — i.e. the order Restate would journal is identical across
executions, so replay lines up positionally.

A `FakeCtx` stands in for the Restate context; its `run_typed` records the command
name at *creation* time (exactly what Restate journals) and returns the coroutine,
so the async I/O still races while the recorded order stays fixed.

Skipped unless `langgraph` is installed (it is an optional `langchain` extra, not a
default test dependency).
"""

import asyncio
import contextvars
import operator
import random
from typing import Annotated, TypedDict

import pytest

pytest.importorskip("langgraph")

from langgraph.graph import StateGraph, START, END  # noqa: E402
from langgraph.types import Send  # noqa: E402

from restate.ext.langchain import durable_scope  # noqa: E402

pytestmark = [pytest.mark.anyio]


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


_journal: contextvars.ContextVar[list] = contextvars.ContextVar("journal")


class FakeCtx:
    """Records command-creation order (what Restate journals); returns the coro."""

    def run_typed(self, name, fn, *args, **kwargs):
        _journal.get().append(name)
        return fn()


class _Inner(TypedDict):
    branch: str
    results: Annotated[list, operator.add]


class _InnerWS(TypedDict):
    branch: str
    leg: str


def _make_sub(dctx):
    async def inner_worker(state: _InnerWS) -> dict:
        b, leg = state["branch"], state["leg"]

        async def fetch():
            await asyncio.sleep(random.uniform(0.005, 0.03))
            return f"f:{b}.{leg}"

        async def store():
            await asyncio.sleep(random.uniform(0.005, 0.03))
            return f"s:{b}.{leg}"

        await dctx.run_typed(f"fetch:{b}.{leg}", fetch)
        await dctx.run_typed(f"store:{b}.{leg}", store)
        return {"results": [f"{b}.{leg}"]}

    def inner_fan(state: _Inner):
        return [Send("inner_worker", {"branch": state["branch"], "leg": leg}) for leg in ("L", "R")]

    b = StateGraph(_Inner)
    b.add_node("inner_worker", inner_worker)
    b.add_conditional_edges(START, inner_fan, ["inner_worker"])
    b.add_edge("inner_worker", END)
    return b.compile()


class _Outer(TypedDict):
    branches: list
    results: Annotated[list, operator.add]


class _OuterWS(TypedDict):
    branch: str


def _build(dctx):
    sub = _make_sub(dctx)

    async def branch_node(state: _OuterWS) -> dict:
        out = await sub.ainvoke({"branch": state["branch"], "results": []})
        return {"results": out["results"]}

    def outer_fan(state: _Outer):
        return [Send("branch_node", {"branch": b}) for b in state["branches"]]

    b = StateGraph(_Outer)
    b.add_node("branch_node", branch_node)
    b.add_conditional_edges(START, outer_fan, ["branch_node"])
    b.add_edge("branch_node", END)
    return b.compile()


async def _one_run() -> tuple:
    journal: list = []
    token = _journal.set(journal)
    try:
        with durable_scope(FakeCtx()) as dctx:
            await _build(dctx).ainvoke({"branches": ["A", "B", "C"], "results": []})
    finally:
        _journal.reset(token)
    return tuple(journal)


async def test_stategraph_durable_order_is_deterministic():
    runs = [await _one_run() for _ in range(20)]
    distinct = set(runs)
    assert len(distinct) == 1, f"non-deterministic journal order: {distinct}"
    # 3 branches x 2 legs x 2 ops = 12 durable commands per run
    assert len(runs[0]) == 12
