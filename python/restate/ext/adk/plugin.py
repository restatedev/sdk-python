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

import restate

from datetime import timedelta
from typing import Optional, Any, cast

from google.genai import types

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.plugins import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.models import LLMRegistry
from google.adk.models.base_llm import BaseLlm
from google.adk.flows.llm_flows.functions import generate_client_function_call_id
from restate.ext.adk import RestateSessionService

from restate.extensions import current_context

from restate.ext.turnstile import Turnstile
from restate.server_context import clear_extension_data, get_extension_data, set_extension_data


def _create_turnstile(s: LlmResponse) -> Turnstile:
    ids = _get_function_call_ids(s)
    turnstile = Turnstile(ids)
    return turnstile


def _invocation_extension_key(invocation_id: str) -> str:
    return "adk_" + invocation_id


def _turnstile_from_context(invocation_id: str) -> Turnstile:
    ctx = current_context()
    if ctx is None:
        raise RuntimeError("No Restate context found, the restate plugin must be used from within a restate handler.")
    state = get_extension_data(ctx, _invocation_extension_key(invocation_id))
    if state is None:
        raise RuntimeError(
            "No RestatePlugin state found, the restate plugin must be used from within a restate handler."
        )
    turnstile = state.turnstiles
    return turnstile


class PluginState:
    def __init__(self, model: BaseLlm):
        self.model = model
        self.turnstiles: Turnstile = Turnstile([])

    def __close__(self):
        """Clean up resources."""
        self.turnstiles.cancel_all()


class RestatePlugin(BasePlugin):
    """A plugin to integrate Restate with the ADK framework."""

    def __init__(self, *, max_model_call_retries: int = 10):
        super().__init__(name="restate_plugin")
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
        set_extension_data(ctx, "adk_" + callback_context.invocation_id, PluginState(model))
        return None

    async def after_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Optional[types.Content]:
        ctx = current_context()
        if ctx is None:
            raise RuntimeError(
                "No Restate context found, the restate plugin must be used from within a restate handler."
            )
        state = get_extension_data(ctx, "adk_" + callback_context.invocation_id)
        if state is not None:
            state.__close__()
        clear_extension_data(ctx, "adk_" + callback_context.invocation_id)

        return None

    async def after_run_callback(self, *, invocation_context: InvocationContext) -> None:
        if isinstance(invocation_context.session_service, RestateSessionService):
            restate_session_service = cast(RestateSessionService, invocation_context.session_service)
            await restate_session_service.flush_session_state(invocation_context.session)

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        ctx = current_context()
        if ctx is None:
            raise RuntimeError(
                "No Restate context found, the restate plugin must be used from within a restate handler."
            )
        state = get_extension_data(ctx, "adk_" + callback_context.invocation_id)
        if state is None:
            raise RuntimeError(
                "No RestatePlugin state found, the restate plugin must be used from within a restate handler."
            )
        model = state.model
        response = await _generate_content_async(ctx, self._max_model_call_retries, model, llm_request)
        turnstile = _create_turnstile(response)
        state.turnstiles = turnstile
        return response

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> Optional[dict]:
        turnstile = _turnstile_from_context(tool_context.invocation_id)
        id = tool_context.function_call_id
        assert id is not None, "Function call ID is required for tool invocation."
        await turnstile.wait_for(id)
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict,
    ) -> Optional[dict]:
        turnstile = _turnstile_from_context(tool_context.invocation_id)
        id = tool_context.function_call_id
        assert id is not None, "Function call ID is required for tool invocation."
        turnstile.allow_next_after(id)

        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> Optional[dict]:
        turnstile = _turnstile_from_context(tool_context.invocation_id)
        id = tool_context.function_call_id
        assert id is not None, "Function call ID is required for tool invocation."
        turnstile.cancel_all_after(id)
        return None


def _get_function_call_ids(s: LlmResponse) -> list[str]:
    ids = []
    if s.content and s.content.parts:
        for part in s.content.parts:
            if part.function_call:
                if part.function_call.id:
                    ids.append(part.function_call.id)
    return ids


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
