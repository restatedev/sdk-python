from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
import dataclasses

from restate import RunOptions, SdkInternalBaseException
from restate.ext.pydantic._utils import current_state
from restate.extensions import current_context
from restate.ext.turnstile import Turnstile

from pydantic_ai.agent.abstract import EventStreamHandler
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelMessage, ModelResponse, ModelResponseStreamEvent
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import AgentDepsT, RunContext
from pydantic_ai.usage import RequestUsage


from ._serde import PydanticTypeAdapter

MODEL_RESPONSE_SERDE = PydanticTypeAdapter(ModelResponse)


class RestateStreamedResponse(StreamedResponse):
    def __init__(self, model_request_parameters: ModelRequestParameters, response: ModelResponse):
        super().__init__(model_request_parameters)
        self.response = response

    async def _get_event_iterator(self) -> AsyncIterator[ModelResponseStreamEvent]:
        return
        # noinspection PyUnreachableCode
        yield

    def get(self) -> ModelResponse:
        return self.response

    def usage(self) -> RequestUsage:
        return self.response.usage  # pragma: no cover

    @property
    def model_name(self) -> str:
        return self.response.model_name or ""  # pragma: no cover

    @property
    def provider_name(self) -> str:
        return self.response.provider_name or ""  # pragma: no cover

    @property
    def timestamp(self) -> datetime:
        return self.response.timestamp  # pragma: no cover

    @property
    def provider_url(self) -> str | None:
        return None


class RestateModelWrapper(WrapperModel):
    def __init__(
        self,
        wrapped: Model,
        run_options: RunOptions,
        event_stream_handler: EventStreamHandler[AgentDepsT] | None = None,
    ):
        super().__init__(wrapped)

        self._options = dataclasses.replace(run_options, serde=MODEL_RESPONSE_SERDE)
        self._event_stream_handler = event_stream_handler

    async def request(self, *args: Any, **kwargs: Any) -> ModelResponse:
        context = current_context()
        if context is None:
            raise UserError(
                "A model cannot be used without a Restate context. Make sure to run it within an agent or a run context."
            )
        try:
            res = await context.run_typed("Model call", self.wrapped.request, self._options, *args, **kwargs)
            ids = [c.tool_call_id for c in res.tool_calls]
            current_state().turnstile = Turnstile(ids)
            return res
        except SdkInternalBaseException as e:
            raise Exception("Internal error during model call") from e

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[AgentDepsT] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        if run_context is None:
            raise UserError(
                "A model cannot be used with `pydantic_ai.direct.model_request_stream()` as it requires a `run_context`. Set an `event_stream_handler` on the agent and use `agent.run()` instead."
            )

        fn = self._event_stream_handler
        assert fn is not None

        async def request_stream_run():
            async with self.wrapped.request_stream(
                messages,
                model_settings,
                model_request_parameters,
                run_context,
            ) as streamed_response:
                await fn(run_context, streamed_response)  # type: ignore[arg-type]

                async for _ in streamed_response:
                    pass
            return streamed_response.get()

        try:
            response = await self._context.run_typed("Model stream call", request_stream_run, self._options)
            yield RestateStreamedResponse(model_request_parameters, response)
        except SdkInternalBaseException as e:
            raise Exception("Internal error during model stream call") from e
