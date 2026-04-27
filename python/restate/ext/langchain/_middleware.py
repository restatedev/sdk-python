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
    """Journaled mirror of `ModelResponse`.

    `result` uses `list[AnyMessage]` (a discriminated union) so concrete
    subclasses — and `AIMessage.tool_calls` in particular — survive the
    round-trip; `BaseMessage` would not. `SchemaT` is parameterized per call
    with the request's structured-output type so `structured_response`
    deserializes back into the user's Pydantic class instead of a dict.
    """

    result: list[AnyMessage]
    structured_response: Optional[SchemaT] = None


class RestateMiddleware(AgentMiddleware):
    """Drop-in middleware that makes a `create_agent` agent durable on Restate.

    Pass it to `create_agent(..., middleware=[RestateMiddleware()])` and run
    the agent inside a Restate handler. LLM responses are journaled; parallel
    tool calls are linearized for deterministic replay.

    Args:
        run_options: forwarded to the LLM `ctx.run_typed` call (max attempts,
            retry intervals, ...). `serde` is set internally on each call.
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

        # Parameterize the journaled type with the structured-output schema
        # (if any) so `structured_response` round-trips back into the user's
        # Pydantic class. `__class_getitem__` is the dynamic form of
        # `RestateModelMessage[schema]` — same thing, but the type checker
        # accepts a runtime value here.
        schema = getattr(request.response_format, "schema", None)
        if isinstance(schema, type):
            try:
                journal_type = RestateModelMessage.__class_getitem__(schema)
            except (TypeError, ValueError, PydanticSchemaGenerationError):
                journal_type = RestateModelMessage[Any]
        else:
            journal_type = RestateModelMessage[Any]

        async def call_model():
            resp = await handler(request)
            # Validate via dict so we don't have to narrow `list[BaseMessage]`
            # to `list[AnyMessage]`; Pydantic picks the right subclass per
            # message via the discriminated union.
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

        # `ToolNode` runs tool calls in parallel via `asyncio.gather`. Seeding
        # the turnstile with the model's tool_call ids lets `awrap_tool_call`
        # release them one at a time in a stable order, so any nested
        # `ctx.run_typed` calls journal deterministically across replays.
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
