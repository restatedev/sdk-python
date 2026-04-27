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
"""LangChain agent middleware that makes a `create_agent`-built agent durable on Restate.

The middleware does two things:

- `awrap_model_call`: journals every LLM call via `ctx.run_typed("call LLM", ...)`.
- `awrap_tool_call`: serializes tool execution via a turnstile keyed on
  `tool_call_id` so concurrent tool calls (which `ToolNode` runs via
  `asyncio.gather`) execute one after another. This keeps the journal order
  of any `ctx.run_typed` calls inside tool bodies deterministic across
  replays.

The middleware does NOT journal tool calls itself, and does NOT intercept
exceptions. Users wrap the side effects they want durable explicitly inside
the tool body via `restate_context().run_typed("name", ...)`. Terminal errors
and SDK suspension signals propagate naturally — LangChain's default
`_default_handle_tool_errors` only catches `ToolInvocationError` (validation)
and re-raises everything else.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Awaitable, Callable, Optional

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from restate import RunOptions
from restate.extensions import current_context
from restate.ext.turnstile import Turnstile

from ._serde import PydanticTypeAdapter
from ._state import current_state

ModelCallResult = ModelResponse | AIMessage | ExtendedModelResponse
ToolCallResult = ToolMessage | Command

# `AnyMessage` is a discriminated union of LangChain's concrete message
# classes. Using `BaseMessage` here would round-trip messages back as plain
# `BaseMessage`s and the agent loop would fail to detect `AIMessage.tool_calls`.
_MODEL_RESPONSE_SERDE: PydanticTypeAdapter[Any] = PydanticTypeAdapter(list[AnyMessage])


def _normalize_to_messages(result: ModelCallResult) -> list[BaseMessage]:
    """Project a model-call result down to the list-of-messages we journal.
    The middleware re-builds the original return shape on replay so the agent
    loop sees the same value either way."""
    if isinstance(result, AIMessage):
        return [result]
    if isinstance(result, ExtendedModelResponse):
        # Commands are not journaled — they are graph control-flow signals
        # and are produced fresh on each replay. We persist only the messages.
        return list(result.model_response.result)
    return list(result.result)


class RestateMiddleware(AgentMiddleware):
    """Drop-in middleware that makes any `create_agent` agent durable on Restate.

    - Journals every model call via `ctx.run_typed("call LLM", ...)`.
    - Serializes parallel tool execution via a turnstile keyed on
      `tool_call_id` so any `ctx.run_typed` calls users place inside tool
      bodies produce a deterministic journal order across replays.

    Wrap tool side effects inside the tool body with Restate context actions
    like `restate_context().run_typed("name", ...)`.

    Args:
        run_options: RunOptions applied to the model `ctx.run_typed` call
            (max attempts, retry intervals, etc.). The `serde` field is
            overridden internally — set the rest as you would for a plain
            `ctx.run_typed` call.
    """

    def __init__(self, run_options: Optional[RunOptions[Any]] = None):
        super().__init__()
        base = run_options or RunOptions()
        self._llm_options: RunOptions[Any] = dataclasses.replace(base, serde=_MODEL_RESPONSE_SERDE)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        ctx = current_context()
        if ctx is None:
            raise RuntimeError(
                "RestateMiddleware must run inside a Restate handler. "
                "Call agent.ainvoke(...) from a handler that exposes a Restate Context."
            )

        async def call_model() -> list[BaseMessage]:
            return _normalize_to_messages(await handler(request))

        messages = await ctx.run_typed("call LLM", call_model, self._llm_options)

        # Seed a turnstile from the model's tool_call ids so the upcoming
        # tool calls — which `ToolNode` may run via asyncio.gather — execute
        # one at a time in a stable order across replays.
        ai = next((m for m in messages if isinstance(m, AIMessage)), None)
        ids = [tc["id"] for tc in (ai.tool_calls if ai else []) if tc.get("id")]
        current_state().turnstile = Turnstile(ids)

        # Re-build the response shape callers expect.
        if len(messages) == 1 and isinstance(messages[0], AIMessage):
            return messages[0]
        return ModelResponse(result=messages)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolCallResult]],
    ) -> ToolCallResult:
        # The middleware does not journal the tool call, but it does serialize
        # parallel tool execution so any `ctx.run_typed` calls users place
        # inside tool bodies produce a stable journal order across replays.
        tool_call = request.tool_call
        tool_call_id: Optional[str] = tool_call.get("id") if isinstance(tool_call, dict) else None
        if tool_call_id is None:
            return await handler(request)

        turnstile = current_state().turnstile
        await turnstile.wait_for(tool_call_id)
        try:
            result = await handler(request)
            turnstile.allow_next_after(tool_call_id)
            return result
        except BaseException:
            # Unblock the rest of the parallel tool batch, then propagate.
            turnstile.cancel_all_after(tool_call_id)
            raise
