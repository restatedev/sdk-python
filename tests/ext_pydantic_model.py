import importlib
import sys
import types
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext

from restate import RunOptions
from restate.context import RunAction
from restate.exceptions import SdkInternalException


pytestmark = [
    pytest.mark.anyio,
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _model_module() -> Any:
    package_name = "restate.ext.pydantic"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(Path(__file__).parents[1] / "python" / "restate" / "ext" / "pydantic")]
        sys.modules[package_name] = package
    return importlib.import_module("restate.ext.pydantic._model")


class FailingContext:
    async def run_typed(
        self,
        name: str,
        action: RunAction[Any],
        opts: RunOptions[Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        raise SdkInternalException()


class DummyModel(Model):
    @property
    def system(self) -> str:
        return "dummy"

    @property
    def model_name(self) -> str:
        return "dummy"

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        return ModelResponse(parts=[])


def _model_request_parameters() -> ModelRequestParameters:
    return ModelRequestParameters(function_tools=[], allow_text_output=True, output_mode="native", output_object=None)


async def test_request_propagates_sdk_internal_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    model_module = _model_module()
    monkeypatch.setattr(model_module, "current_context", lambda: FailingContext())
    wrapper = model_module.RestateModelWrapper(DummyModel(), RunOptions())

    with pytest.raises(SdkInternalException):
        await wrapper.request([], None, _model_request_parameters())


async def test_request_stream_propagates_sdk_internal_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    model_module = _model_module()
    monkeypatch.setattr(model_module, "current_context", lambda: FailingContext())
    wrapper = model_module.RestateModelWrapper(
        DummyModel(),
        RunOptions(),
        event_stream_handler=lambda run_context, streamed_response: None,
    )

    with pytest.raises(SdkInternalException):
        async with wrapper.request_stream(
            [],
            None,
            _model_request_parameters(),
            cast(RunContext[Any], object()),
        ):
            pass
