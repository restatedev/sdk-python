from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import ModelResponse, TResponseInputItem, TResponseStreamEvent
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelTracing
from agents.tool import Tool
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText
from openai.types.responses.response_prompt_param import ResponsePromptParam

class ScriptedModel(Model):
    """Returns valid OpenAI Agents SDK responses for each test scenario."""

    cities = ("Paris", "London", "Tokyo", "Berlin", "Rome")

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff[Any, Any]],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> ModelResponse:
        tool_names = {tool.name for tool in tools}
        input_text = self._input_text(input)

        if "get_weather" in tool_names:
            cities = [city for city in self.cities if city in input_text]
            if not cities:
                return self._text_response("Done.")
            call_ids = {f"weather-{city.lower()}" for city in cities}
            if self._has_tool_output(input, call_ids):
                return self._text_response("Done.")
            return self._tool_response(*(("get_weather", {"city": city}, f"weather-{city.lower()}") for city in cities))

        if "explode" in tool_names:
            return self._tool_response(("explode", {"reason": "scripted failure"}, "explode"))

        if "ask_specialist" in tool_names:
            if self._has_tool_output(input, {"ask-specialist"}):
                return self._text_response("Done.")
            return self._tool_response(
                ("ask_specialist", {"question": "How do I speed up a slow SQL query?"}, "ask-specialist")
            )

        if handoffs:
            return self._tool_response((handoffs[0].tool_name, {}, "billing-handoff"))

        return self._text_response("Done.")

    async def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[TResponseStreamEvent]:
        raise AssertionError("ScriptedModel only supports Runner.run")
        yield

    @staticmethod
    def _input_text(input: str | list[TResponseInputItem]) -> str:
        return input if isinstance(input, str) else json.dumps(input)

    @staticmethod
    def _has_tool_output(input: str | list[TResponseInputItem], call_ids: set[str]) -> bool:
        return isinstance(input, list) and any(
            isinstance(item, dict) and item.get("type") == "function_call_output" and item.get("call_id") in call_ids
            for item in input
        )

    @staticmethod
    def _tool_response(*calls: tuple[str, dict[str, str], str]) -> ModelResponse:
        return ModelResponse(
            output=[
                ResponseFunctionToolCall(
                    arguments=json.dumps(arguments),
                    call_id=call_id,
                    id=f"function-{call_id}",
                    name=name,
                    type="function_call",
                )
                for name, arguments, call_id in calls
            ],
            usage=Usage(),
            response_id="scripted-response",
        )

    @staticmethod
    def _text_response(text: str) -> ModelResponse:
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="scripted-message",
                    content=[ResponseOutputText(annotations=[], text=text, type="output_text")],
                    role="assistant",
                    status="completed",
                    type="message",
                )
            ],
            usage=Usage(),
            response_id="scripted-response",
        )
