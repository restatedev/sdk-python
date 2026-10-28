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
"""Unit tests for the pydantic-ai integration, driven by a fake Restate context.

The fake context executes each `run_typed` action and round-trips its result through the configured
serde, which is what a journaled `ctx.run` does on replay.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Iterator
from typing import Any

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, ModelRetry  # noqa: E402
from pydantic_ai.messages import (  # noqa: E402
    ModelMessage,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturn,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel  # noqa: E402
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults  # noqa: E402

from restate import RunOptions, TerminalError  # noqa: E402
from restate.exceptions import SdkInternalException  # noqa: E402
from restate.ext.pydantic import RestateAgent  # noqa: E402
from restate.ext.pydantic._model import RestateModelWrapper  # noqa: E402
from restate.ext.turnstile import Turnstile  # noqa: E402
from restate.server_context import _restate_context_var  # noqa: E402


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [
    pytest.mark.anyio,
]


class FakeContext:
    def __init__(self) -> None:
        self.run_names: list[str] = []

    async def run_typed(self, name: str, action: Any, options: RunOptions | None = None, *args: Any, **kwargs: Any):
        self.run_names.append(name)
        result = action(*args, **kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        if options is None or options.serde is None:
            return result
        return options.serde.deserialize(options.serde.serialize(result))


@pytest.fixture
def fake_context() -> Iterator[FakeContext]:
    ctx = FakeContext()
    token = _restate_context_var.set(ctx)  # type: ignore[arg-type]
    try:
        yield ctx
    finally:
        _restate_context_var.reset(token)


def _has_tool_return(messages: list[ModelMessage]) -> bool:
    return any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts)


def _tool_calls_then_text(*calls: ToolCallPart) -> FunctionModel:
    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if _has_tool_return(messages):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=list(calls))

    return FunctionModel(model_fn)


def _tool_returns(messages: list[ModelMessage]) -> dict[str, ToolReturnPart]:
    return {
        part.tool_call_id: part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)
    }


async def test_tool_return_survives_journal_round_trip(fake_context: FakeContext):
    agent = Agent(_tool_calls_then_text(ToolCallPart("lookup", {}, tool_call_id="c1")))

    @agent.tool_plain
    def lookup() -> ToolReturn:
        return ToolReturn(return_value={"temp": 21}, content="extra context", metadata={"source": "sensor"})

    result = await RestateAgent(agent, auto_wrap_tools=True).run("go")

    messages = result.all_messages()
    part = _tool_returns(messages)["c1"]
    assert part.content == {"temp": 21}
    assert part.metadata == {"source": "sensor"}
    user_prompts = [p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)]
    assert "extra context" in user_prompts
    assert "Calling lookup" in fake_context.run_names


async def test_plain_tool_output_still_round_trips(fake_context: FakeContext):
    agent = Agent(_tool_calls_then_text(ToolCallPart("lookup", {}, tool_call_id="c1")))

    @agent.tool_plain
    def lookup() -> dict[str, int]:
        return {"temp": 21}

    result = await RestateAgent(agent, auto_wrap_tools=True).run("go")

    part = _tool_returns(result.all_messages())["c1"]
    assert part.content == {"temp": 21}
    assert part.metadata is None


async def test_call_deferred_metadata_is_preserved(fake_context: FakeContext):
    agent = Agent(
        _tool_calls_then_text(ToolCallPart("slow", {}, tool_call_id="c1")),
        output_type=[str, DeferredToolRequests],
    )

    @agent.tool_plain
    def slow() -> str:
        raise CallDeferred(metadata={"job": "42"})

    result = await RestateAgent(agent, auto_wrap_tools=True).run("go")

    assert isinstance(result.output, DeferredToolRequests)
    assert [c.tool_call_id for c in result.output.calls] == ["c1"]
    assert result.output.metadata == {"c1": {"job": "42"}}


async def test_parallel_approval_required_does_not_abort_sibling(fake_context: FakeContext):
    agent = Agent(
        _tool_calls_then_text(
            ToolCallPart("dangerous", {}, tool_call_id="c1"),
            ToolCallPart("safe", {}, tool_call_id="c2"),
        ),
        output_type=[str, DeferredToolRequests],
    )

    @agent.tool_plain
    def dangerous() -> str:
        raise ApprovalRequired(metadata={"reason": "destructive"})

    @agent.tool_plain
    def safe() -> str:
        return "ok"

    result = await RestateAgent(agent, auto_wrap_tools=True).run("go")

    assert isinstance(result.output, DeferredToolRequests)
    assert [c.tool_call_id for c in result.output.approvals] == ["c1"]
    assert result.output.metadata == {"c1": {"reason": "destructive"}}
    assert _tool_returns(result.all_messages())["c2"].content == "ok"


async def test_parallel_call_deferred_does_not_abort_sibling(fake_context: FakeContext):
    agent = Agent(
        _tool_calls_then_text(
            ToolCallPart("slow", {}, tool_call_id="c1"),
            ToolCallPart("safe", {}, tool_call_id="c2"),
        ),
        output_type=[str, DeferredToolRequests],
    )

    @agent.tool_plain
    def slow() -> str:
        raise CallDeferred(metadata={"job": "42"})

    @agent.tool_plain
    def safe() -> str:
        return "ok"

    result = await RestateAgent(agent, auto_wrap_tools=True).run("go")

    assert isinstance(result.output, DeferredToolRequests)
    assert [c.tool_call_id for c in result.output.calls] == ["c1"]
    assert result.output.metadata == {"c1": {"job": "42"}}
    assert _tool_returns(result.all_messages())["c2"].content == "ok"


async def test_parallel_model_retry_does_not_abort_sibling(fake_context: FakeContext):
    seen: list[list[ModelMessage]] = []

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        if len(seen) > 1:
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[
                ToolCallPart("flaky", {}, tool_call_id="c1"),
                ToolCallPart("safe", {}, tool_call_id="c2"),
            ]
        )

    agent = Agent(FunctionModel(model_fn))
    calls: list[str] = []

    @agent.tool_plain
    def flaky() -> str:
        calls.append("flaky")
        raise ModelRetry("try again later")

    @agent.tool_plain
    def safe() -> str:
        calls.append("safe")
        return "ok"

    result = await RestateAgent(agent, auto_wrap_tools=True).run("go")

    assert result.output == "done"
    assert calls == ["flaky", "safe"]
    retry_request = seen[1][-1]
    retries = [p for p in retry_request.parts if isinstance(p, RetryPromptPart)]
    returns = [p for p in retry_request.parts if isinstance(p, ToolReturnPart)]
    assert [(p.tool_call_id, p.content) for p in retries] == [("c1", "try again later")]
    assert [(p.tool_call_id, p.content) for p in returns] == [("c2", "ok")]


async def test_request_stream_initializes_turnstile(fake_context: FakeContext):
    async def stream_fn(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        if _has_tool_return(messages):
            yield "done"
        else:
            yield {0: DeltaToolCall(name="lookup", json_args="{}", tool_call_id="c1")}
            yield {1: DeltaToolCall(name="lookup", json_args="{}", tool_call_id="c2")}

    async def handler(ctx: Any, events: AsyncIterable[Any]) -> None:
        async for _ in events:
            pass

    agent = Agent(FunctionModel(stream_function=stream_fn))
    calls: list[str] = []

    @agent.tool_plain
    def lookup() -> str:
        calls.append("lookup")
        return "ok"

    result = await RestateAgent(agent, auto_wrap_tools=True, event_stream_handler=handler).run("go")

    assert result.output == "done"
    assert calls == ["lookup", "lookup"]
    assert "Model stream call" in fake_context.run_names
    assert set(_tool_returns(result.all_messages())) == {"c1", "c2"}


async def test_resumed_deferred_call_runs_without_model_request(fake_context: FakeContext):
    agent = Agent(
        _tool_calls_then_text(ToolCallPart("dangerous", {}, tool_call_id="c1")),
        output_type=[str, DeferredToolRequests],
    )

    @agent.tool_plain(requires_approval=True)
    def dangerous() -> str:
        return "deleted"

    restate_agent = RestateAgent(agent, auto_wrap_tools=True)
    first = await restate_agent.run("go")
    assert isinstance(first.output, DeferredToolRequests)

    resumed = await restate_agent.run(
        message_history=first.all_messages(),
        deferred_tool_results=DeferredToolResults(approvals={"c1": True}),
    )

    assert resumed.output == "done"
    assert _tool_returns(resumed.all_messages())["c1"].content == "deleted"


async def test_turnstile_lets_untracked_id_through():
    turnstile = Turnstile(["a", "b"])

    await asyncio.wait_for(turnstile.wait_for("nested"), timeout=1)
    turnstile.allow_next_after("nested")
    turnstile.cancel_all_after("nested")

    assert not turnstile.canceled
    await asyncio.wait_for(turnstile.wait_for("a"), timeout=1)


async def test_turnstile_untracked_id_raises_after_cancel():
    turnstile = Turnstile(["a", "b"])
    turnstile.cancel_all_after("a")

    with pytest.raises(SdkInternalException):
        await turnstile.wait_for("nested")


async def test_turnstile_allow_next_after_deferred_unblocks_sibling():
    turnstile = Turnstile(["a", "b"])
    sibling = asyncio.create_task(turnstile.wait_for("b"))
    await asyncio.sleep(0)
    assert not sibling.done()

    turnstile.allow_next_after("a")

    await asyncio.wait_for(sibling, timeout=1)


async def test_iter_uses_durable_model_and_toolsets(fake_context: FakeContext):
    agent = Agent(_tool_calls_then_text(ToolCallPart("lookup", {}, tool_call_id="c1")))

    @agent.tool_plain
    def lookup() -> str:
        return "ok"

    restate_agent = RestateAgent(agent, auto_wrap_tools=True)
    async with restate_agent.iter("go") as agent_run:
        async for _ in agent_run:
            pass

    assert agent_run.result is not None
    assert agent_run.result.output == "done"
    assert fake_context.run_names.count("Model call") == 2
    assert "Calling lookup" in fake_context.run_names
    assert isinstance(restate_agent.model, RestateModelWrapper)


async def test_iter_rejects_run_time_model(fake_context: FakeContext):
    agent = Agent(_tool_calls_then_text())

    with pytest.raises(TerminalError):
        async with RestateAgent(agent).iter("go", model=_tool_calls_then_text()):
            pass
