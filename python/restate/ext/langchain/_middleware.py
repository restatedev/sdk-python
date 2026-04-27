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
from typing import Any, Awaitable, Callable, Generic, Optional, TypeVar

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from pydantic import BaseModel
from pydantic.errors import PydanticSchemaGenerationError

from restate import RunOptions
from restate.extensions import current_context
from restate.ext.turnstile import Turnstile

from ._serde import PydanticTypeAdapter
from ._state import current_state

ModelCallResult = ModelResponse | AIMessage | ExtendedModelResponse
ToolCallResult = ToolMessage | Command

SchemaT = TypeVar("SchemaT")


class RestateModelMessage(BaseModel, Generic[SchemaT]):
    """Pydantic shape we journal in place of `ModelResponse`.

    `result` is `list[AnyMessage]` — the discriminated union of LangChain's
    concrete message classes — so AIMessage subclass info (and `tool_calls`)
    survives the round-trip, and we still cover the `ToolMessage` that
    LangChain may emit alongside the AIMessage for tool-based structured
    output. `Optional[SchemaT]` pins the user's structured-output Pydantic
    class so it round-trips back into the same type on replay.
    """

    result: list[AnyMessage]
    structured_response: Optional[SchemaT] = None


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
        self._base_options: RunOptions[Any] = run_options or RunOptions()

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

        schema = getattr(request.response_format, "schema", None)
        if isinstance(schema, type):
            try:
                # Pydantic supports parameterizing generics with a runtime type;
                # the type checker can't see past the dynamic subscript.
                journal_type = RestateModelMessage.__class_getitem__(schema)
            except (TypeError, ValueError, PydanticSchemaGenerationError):
                journal_type = RestateModelMessage[Any]
        else:
            journal_type = RestateModelMessage[Any]

        async def call_model():
            resp = await handler(request)
            # `model_validate` accepts an untyped dict so we sidestep the
            # `BaseMessage` -> `AnyMessage` narrowing complaint; Pydantic
            # does the discriminated-union dispatch on `result` itself.
            return journal_type.model_validate(
                {
                    "result": resp.result,
                    "structured_response": resp.structured_response,
                }
            )

        options = dataclasses.replace(
            self._base_options,
            serde=PydanticTypeAdapter(journal_type),
        )

        journaled = await ctx.run_typed("LLM call", call_model, options)

        # Seed a turnstile from the model's tool_call ids so the upcoming
        # tool calls — which `ToolNode` may run via asyncio.gather — execute
        # one at a time in a stable order across replays.
        ai = next((m for m in journaled.result if isinstance(m, AIMessage)), None)
        ids = [tc["id"] for tc in (ai.tool_calls if ai else []) if tc.get("id")]
        current_state().turnstile = Turnstile(ids)

        return ModelResponse(
            result=list(journaled.result),
            structured_response=journaled.structured_response,
        )

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
        try:
            await turnstile.wait_for(tool_call_id)
            result = await handler(request)
            turnstile.allow_next_after(tool_call_id)
            return result
        except BaseException:
            # Unblock the rest of the parallel tool batch, then propagate.
            turnstile.cancel_all_after(tool_call_id)
            raise
