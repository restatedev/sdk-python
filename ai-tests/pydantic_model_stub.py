from __future__ import annotations

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel


class ScriptedPydanticModel(FunctionModel):
    """Returns deterministic Pydantic AI messages for every test scenario."""

    cities = ("Paris", "London", "Tokyo", "Berlin", "Rome")

    def __init__(self) -> None:
        super().__init__(self._response, model_name="scripted-pydantic")

    def _response(self, messages: list[ModelMessage], agent_info: AgentInfo) -> ModelResponse:
        tool_names = {tool.name for tool in agent_info.function_tools}
        prompt = self._latest_prompt(messages)

        if "In which country is it?" in prompt:
            return self._text_response("France")

        if "What is the capital of France?" in prompt:
            return self._text_response("Paris")

        if "get_weather" in tool_names:
            cities = [city for city in self.cities if city in prompt]
            if not cities:
                return self._text_response("Done.")
            call_ids = {f"weather-{city.lower()}" for city in cities}
            if self._has_tool_returns(messages, call_ids):
                return self._text_response("Done.")
            return ModelResponse(
                parts=[
                    ToolCallPart("get_weather", {"city": city}, tool_call_id=f"weather-{city.lower()}")
                    for city in cities
                ]
            )

        if "explode" in tool_names:
            return ModelResponse(
                parts=[ToolCallPart("explode", {"reason": "scripted failure"}, tool_call_id="explode")]
            )

        if "ask_specialist" in tool_names:
            if self._has_tool_returns(messages, {"ask-specialist"}):
                return self._text_response("Done.")
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "ask_specialist",
                        {"question": "How do I speed up a slow SQL query?"},
                        tool_call_id="ask-specialist",
                    )
                ]
            )

        if "handoff_to_billing" in tool_names:
            if self._has_tool_returns(messages, {"billing-handoff"}):
                return self._text_response("Done.")
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "handoff_to_billing",
                        {"question": "I was double charged on my invoice."},
                        tool_call_id="billing-handoff",
                    )
                ]
            )

        return self._text_response("Done.")

    @staticmethod
    def _latest_prompt(messages: list[ModelMessage]) -> str:
        for message in reversed(messages):
            if isinstance(message, ModelRequest):
                for part in reversed(message.parts):
                    if isinstance(part, UserPromptPart):
                        return part.content if isinstance(part.content, str) else str(part.content)
        return ""

    @staticmethod
    def _has_tool_returns(messages: list[ModelMessage], call_ids: set[str]) -> bool:
        return any(
            isinstance(part, ToolReturnPart) and part.tool_call_id in call_ids
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )

    @staticmethod
    def _text_response(text: str) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)])
