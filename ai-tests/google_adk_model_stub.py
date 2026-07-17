from __future__ import annotations

from collections.abc import AsyncGenerator

from google.adk.models.google_llm import Gemini
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

CITIES = ("Paris", "London", "Tokyo", "Berlin", "Rome")


async def scripted_generate_content_async(
    model: Gemini,
    llm_request: LlmRequest,
    stream: bool = False,
) -> AsyncGenerator[LlmResponse, None]:
    """Replace only the Gemini provider call while retaining normal ADK setup."""
    del model, stream
    tool_names = set(llm_request.tools_dict)
    prompt = _latest_prompt(llm_request)

    if "In which country is it?" in prompt:
        yield _text_response("France")
        return

    if "What is the capital of France?" in prompt:
        yield _text_response("Paris")
        return

    if "get_weather" in tool_names:
        cities = [city for city in CITIES if city in prompt]
        if not cities:
            yield _text_response("Done.")
            return
        call_ids = {f"weather-{city.lower()}" for city in cities}
        if _has_function_responses(llm_request, call_ids):
            yield _text_response("Done.")
            return
        yield LlmResponse(
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id=f"weather-{city.lower()}",
                            name="get_weather",
                            args={"city": city},
                        )
                    )
                    for city in cities
                ],
            )
        )
        return

    if "explode" in tool_names:
        yield _tool_call("explode", {"reason": "scripted failure"}, "explode")
        return

    if "ask_specialist" in tool_names:
        if _has_function_responses(llm_request, {"ask-specialist"}):
            yield _text_response("Done.")
        else:
            yield _tool_call(
                "ask_specialist",
                {"question": "How do I speed up a slow SQL query?"},
                "ask-specialist",
            )
        return

    if "transfer_to_agent" in tool_names:
        if _has_function_responses(llm_request, {"billing-handoff"}):
            yield _text_response("Done.")
        else:
            yield _tool_call(
                "transfer_to_agent",
                {"agent_name": "billing_agent"},
                "billing-handoff",
            )
        return

    yield _text_response("Done.")


def _latest_prompt(llm_request: LlmRequest) -> str:
    for content in reversed(llm_request.contents):
        if content.role != "user":
            continue
        for part in reversed(content.parts or []):
            if part.text:
                return part.text
    return ""


def _has_function_responses(llm_request: LlmRequest, call_ids: set[str]) -> bool:
    return any(
        part.function_response is not None and part.function_response.id in call_ids
        for content in llm_request.contents
        for part in content.parts or []
    )


def _text_response(text: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part.from_text(text=text)]))


def _tool_call(name: str, args: dict[str, str], call_id: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(function_call=types.FunctionCall(id=call_id, name=name, args=args))],
        )
    )
