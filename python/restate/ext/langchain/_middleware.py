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

from dataclasses import asdict
from typing import Any, Awaitable, Callable, Optional, cast

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage, BaseMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from pydantic import BaseModel

from restate import RunOptions
from restate.extensions import current_context
from restate.ext.turnstile import Turnstile

from ._state import get_or_create_state, state_from_ctx

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
            response = await handler(request)
            return SerializableModelResponse(**asdict(response))

        journaled = await ctx.run_typed("LLM call", call_model, self._options)

        # If the request asked for a Pydantic schema, restore the type.
        structured_response = journaled.structured_response
        schema = getattr(request.response_format, "schema", None)
        if structured_response is not None and isinstance(schema, type) and issubclass(schema, BaseModel):
            structured_response = schema.model_validate(structured_response)

        # Install this turn's turnstile on ctx.extension_data so sibling
        # ``awrap_tool_call`` tasks (spawned by ``tool_node``'s gather) reach
        # the same object via the shared Restate ``Context`` — independent of
        # ContextVar inheritance. Mirrors how ``restate.ext.adk`` stores its
        # PluginState turnstile.
        ai_message = next((m for m in journaled.result if isinstance(m, AIMessage)), None)
        state = get_or_create_state(ctx)
        if ai_message is not None:
            tool_call_ids = [tid for tc in (ai_message.tool_calls or []) if (tid := tc.get("id")) is not None]
            state.turnstile = Turnstile(tool_call_ids)

        # Turn into ModelResponse as expected by the agent
        return ModelResponse(
            result=cast(list[BaseMessage], journaled.result),
            structured_response=structured_response,
        )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolCallResult]],
    ) -> ToolCallResult:
        tool_call_id = request.tool_call.get("id")
        if tool_call_id is None:
            return ToolMessage("Function call ID is required for tool invocation. Please set `id` on the ToolCall.")

        ctx = current_context()
        assert ctx is not None, "RestateMiddleware must run inside a Restate handler"
        state = state_from_ctx(ctx)
        assert state is not None, "RestateMiddleware must run inside a Restate handler"
        turnstile = state.turnstile

        try:
            await turnstile.wait_for(tool_call_id)
            result = await handler(request)
            turnstile.allow_next_after(tool_call_id)
            result.id = str(ctx.uuid())
            return result
        except BaseException:
            # Unblock the rest of the parallel tool batch, then propagate.
            turnstile.cancel_all_after(tool_call_id)
            raise
