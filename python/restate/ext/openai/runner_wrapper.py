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
This module contains the optional OpenAI integration for Restate.
"""

import dataclasses

from agents import (
    Model,
    RunConfig,
    TContext,
    RunResult,
    Agent,
    Runner,
)
from agents.models.multi_provider import MultiProvider
from agents.items import TResponseStreamEvent, ModelResponse
from agents.items import TResponseInputItem
from typing import AsyncIterator

from restate.ext.turnstile import Turnstile
from restate.extensions import current_context
from restate import RunOptions, TerminalError

from .functions import get_function_call_ids, wrap_agent_tools
from .models import LlmRetryOpts, RestateModelResponse, State
from .session import RestateSession


class DurableModelCalls(MultiProvider):
    """
    A Restate model provider that wraps the OpenAI SDK's default MultiProvider.
    """

    def __init__(self, state: State, llm_retry_opts: LlmRetryOpts | None = None):
        super().__init__()
        self.llm_retry_opts = llm_retry_opts
        self.state = state

    def get_model(self, model_name: str | None) -> Model:
        model = super().get_model(model_name or None)
        return RestateModelWrapper(model, self.state, self.llm_retry_opts)


class RestateModelWrapper(Model):
    """
    A wrapper around the OpenAI SDK's Model that persists LLM calls in the Restate journal.
    """

    def __init__(self, model: Model, state: State, llm_retry_opts: LlmRetryOpts | None = None):
        self.model = model
        self.state = state
        self.model_name = "RestateModelWrapper"
        self.llm_retry_opts = llm_retry_opts if llm_retry_opts is not None else LlmRetryOpts()

    async def get_response(self, *args, **kwargs) -> ModelResponse:
        async def call_llm() -> RestateModelResponse:
            resp = await self.model.get_response(*args, **kwargs)
            # convert to pydantic model to be serializable by Restate SDK
            return RestateModelResponse(
                output=resp.output,
                usage=resp.usage,
                response_id=resp.response_id,
            )

        ctx = current_context()
        if ctx is None:
            raise RuntimeError("No current Restate context found, make sure to run inside a Restate handler")
        result = await ctx.run_typed(
            "call LLM",
            call_llm,
            RunOptions(
                max_attempts=self.llm_retry_opts.max_attempts,
                max_duration=self.llm_retry_opts.max_duration,
                initial_retry_interval=self.llm_retry_opts.initial_retry_interval,
                max_retry_interval=self.llm_retry_opts.max_retry_interval,
                retry_interval_factor=self.llm_retry_opts.retry_interval_factor,
            ),
        )
        # collect function call IDs, to
        ids = get_function_call_ids(result.output)
        self.state.turnstile = Turnstile(ids)

        # convert back to original ModelResponse
        return ModelResponse(
            output=result.output,
            usage=result.usage,
            response_id=result.response_id,
        )

    def stream_response(self, *args, **kwargs) -> AsyncIterator[TResponseStreamEvent]:
        raise TerminalError("Streaming is not supported in Restate. Use `get_response` instead.")


class DurableRunner:
    """
    A wrapper around Runner.run that automatically configures RunConfig for Restate contexts.

    This class automatically sets up the appropriate model provider (DurableModelCalls) and
    model settings, taking over any model and model_settings configuration provided in the
    original RunConfig.
    """

    @staticmethod
    async def run(
        starting_agent: Agent[TContext],
        input: str | list[TResponseInputItem],
        *,
        use_restate_session: bool = False,
        **kwargs,
    ) -> RunResult:
        """
        Run an agent with automatic Restate configuration.

        Args:
            use_restate_session: If True, creates a RestateSession for conversation persistence.
                                Requires running within a Restate Virtual Object context.

        Returns:
            The result from Runner.run
        """

        # execution state
        state = State()

        # Set persisting model calls
        llm_retry_opts = kwargs.pop("llm_retry_opts", None)
        run_config = kwargs.pop("run_config", RunConfig())
        run_config = dataclasses.replace(run_config, model_provider=DurableModelCalls(state, llm_retry_opts))

        # Use Restate session if requested, otherwise use provided session
        session = kwargs.pop("session", None)
        if use_restate_session:
            if session is not None:
                raise TerminalError("When use_restate_session is True, session config cannot be provided.")
            session = RestateSession()

        agent = wrap_agent_tools(starting_agent, state)
        try:
            result = await Runner.run(
                starting_agent=agent, input=input, run_config=run_config, session=session, **kwargs
            )
        finally:
            # Flush session items to Restate
            if session is not None and isinstance(session, RestateSession):
                session.flush()

        return result
