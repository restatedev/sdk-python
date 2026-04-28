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
"""LangChain agent middleware that makes a `create_agent` agent durable on Restate.

- `awrap_model_call` journals each LLM response so retries replay it from the
  journal instead of re-calling the model.
- `awrap_tool_call` runs parallel tool calls one at a time (via a turnstile
  keyed on `tool_call_id`) so any `ctx.run_typed(...)` calls users place in
  tool bodies appear in the journal in a stable order across replays.

The middleware does not journal tool calls itself and does not catch
exceptions — wrap side effects explicitly with `restate_context().run_typed(...)`
inside the tool body.
"""

from typing import Any, Awaitable, Callable, Optional

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from pydantic import BaseModel

from restate import RunOptions
from restate.extensions import current_context
from restate.ext.turnstile import Turnstile

from ._state import current_state

ToolCallResult = ToolMessage | Command


class SerializableModelResponse(BaseModel):
    """Serializable mirror of `ModelResponse`.

    `result` uses `list[AnyMessage]` (a discriminated union)
    so AIMessage `tool_calls` survives serialization.
    `BaseMessage`, as on `ModelResponse`, would not.
    """

    result: list[AnyMessage]
    structured_response: Optional[Any] = None


class RestateMiddleware(AgentMiddleware):
    """Drop-in middleware that makes a `create_agent` agent durable on Restate.

    Pass it to `create_agent(..., middleware=[RestateMiddleware()])` and run
    the agent inside a Restate handler. LLM responses are journaled; parallel
    tool calls are linearized for deterministic replay.

    Args:
        run_options: forwarded to the LLM `ctx.run_typed` call (max attempts,
            retry intervals, ...). `serde` is set internally.
    """

    def __init__(self, run_options: Optional[RunOptions[Any]] = None):
        super().__init__()
        self._options: RunOptions[Any] = run_options or RunOptions()

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        ctx = current_context()
        if ctx is None:
            raise RuntimeError(
                "RestateMiddleware must run inside a Restate handler. "
                "Call agent.ainvoke(...) from a handler that exposes a Restate Context."
            )

        async def call_model() -> SerializableModelResponse:
            resp = await handler(request)
            # Serialize the response to a dict so we can journal it.
            sr = resp.structured_response
            if isinstance(sr, BaseModel):
                sr = sr.model_dump(mode="json")
            return SerializableModelResponse.model_validate({"result": resp.result, "structured_response": sr})

        journaled = await ctx.run_typed("LLM call", call_model, self._options)

        # If the request asked for a Pydantic schema, restore the type
        sr = journaled.structured_response
        schema = getattr(request.response_format, "schema", None)
        if sr is not None and isinstance(schema, type) and issubclass(schema, BaseModel):
            sr = schema.model_validate(sr)

        # Force tools to run sequentially by setting a turnstile.
        # Avoids asyncio.gather() from running in parallel.
        ai_message = next((m for m in journaled.result if isinstance(m, AIMessage)), None)
        if ai_message:
            tool_call_ids = [tc.get("id") for tc in (ai_message.tool_calls or []) if tc.get("id") is not None]
            current_state().turnstile = Turnstile(tool_call_ids)

        return ModelResponse(result=journaled.result, structured_response=sr)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolCallResult]],
    ) -> ToolCallResult:
        tool_call = request.tool_call
        tool_call_id: Optional[str] = tool_call.get("id") if isinstance(tool_call, dict) else None
        if tool_call_id is None:
            return await handler(request)

        # Wait for turn and then execute
        turnstile = current_state().turnstile
        try:
            await turnstile.wait_for(tool_call_id)
            result = await handler(request)
            turnstile.allow_next_after(tool_call_id)
            return result
        except BaseException:
            # Unblock the rest of the parallel tool batch, then propagate.
            turnstile.cancel_all_after(tool_call_id)
            raise
