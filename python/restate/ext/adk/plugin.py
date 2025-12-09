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
ADK plugin implementation for restate.
"""

import asyncio
import restate

from datetime import timedelta
from typing import Optional, Any, cast

from google.genai import types

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.models import LLMRegistry
from google.adk.models.base_llm import BaseLlm
from google.adk.flows.llm_flows.functions import generate_client_function_call_id


from restate.extensions import current_context

from .session import flush_session_state


class RestatePlugin(BasePlugin):
    """A plugin to integrate Restate with the ADK framework."""

    _models: dict[str, BaseLlm]
    _locks: dict[str, asyncio.Lock]

    def __init__(self, *, max_model_call_retries: int = 10):
        super().__init__(name="restate_plugin")
        self._models = {}
        self._locks = {}
        self._max_model_call_retries = max_model_call_retries

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Optional[types.Content]:
        if not isinstance(agent, LlmAgent):
            raise restate.TerminalError("RestatePlugin only supports LlmAgent agents.")
        ctx = current_context()  # Ensure we have a Restate context
        if ctx is None:
            raise restate.TerminalError(
                """No Restate context found for RestatePlugin.
            Ensure that the agent is invoked within a restate handler and,
            using a ```with restate_overrides(ctx):``` block. around your agent use."""
            )
        model = agent.model if isinstance(agent.model, BaseLlm) else LLMRegistry.new_llm(agent.model)
        self._models[callback_context.invocation_id] = model
        self._locks[callback_context.invocation_id] = asyncio.Lock()

        id = callback_context.invocation_id
        event = ctx.request().attempt_finished_event

        async def release_task():
            """make sure to release resources when the agent finishes"""
            try:
                await event.wait()
            finally:
                self._models.pop(id, None)
                self._locks.pop(id, None)

        _ = asyncio.create_task(release_task())
        return None

    async def after_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Optional[types.Content]:
        self._models.pop(callback_context.invocation_id, None)
        self._locks.pop(callback_context.invocation_id, None)

        ctx = cast(restate.ObjectContext, current_context())
        await flush_session_state(ctx, callback_context.session)

        return None

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        model = self._models[callback_context.invocation_id]
        ctx = current_context()
        if ctx is None:
            raise RuntimeError(
                "No Restate context found, the restate plugin must be used from within a restate handler."
            )
        response = await _generate_content_async(ctx, self._max_model_call_retries, model, llm_request)
        return response

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict]:
        tool_context.session.state["restate_context"] = current_context()
        lock = self._locks[tool_context.invocation_id]
        await lock.acquire()
        # TODO: if we want we can also automatically wrap tools with ctx.run_typed here
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> Optional[dict]:
        lock = self._locks[tool_context.invocation_id]
        lock.release()
        tool_context.session.state.pop("restate_context", None)
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> Optional[dict]:
        lock = self._locks[tool_context.invocation_id]
        lock.release()
        tool_context.session.state.pop("restate_context", None)
        return None

    async def close(self):
        self._models.clear()
        self._locks.clear()


def _generate_client_function_call_id(s: LlmResponse) -> None:
    """Generate client function call IDs for function calls in the LlmResponse.
    It is important for the function call IDs to be stable across retries, as they
    are used to correlate function call results with their invocations.
    """
    if s.content and s.content.parts:
        for part in s.content.parts:
            if part.function_call:
                if not part.function_call.id:
                    id = generate_client_function_call_id()
                    part.function_call.id = id


async def _generate_content_async(
    ctx: restate.Context, max_attempts: int, model: BaseLlm, llm_request: LlmRequest
) -> LlmResponse:
    """Generate content using Restate's context."""

    async def call_llm() -> LlmResponse:
        a_gen = model.generate_content_async(llm_request, stream=False)
        try:
            result = await anext(a_gen)
            _generate_client_function_call_id(result)
            return result
        finally:
            await a_gen.aclose()

    return await ctx.run_typed(
        "call LLM",
        call_llm,
        restate.RunOptions(max_attempts=max_attempts, initial_retry_interval=timedelta(seconds=1)),
    )
