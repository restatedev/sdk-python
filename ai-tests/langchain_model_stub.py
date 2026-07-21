from __future__ import annotations

from typing import Any

from langchain_core.callbacks.manager import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

CITIES = ("Paris", "London", "Tokyo", "Berlin", "Rome")


async def scripted_agenerate(
    model: ChatOpenAI,
    messages: list[BaseMessage],
    stop: list[str] | None = None,
    run_manager: AsyncCallbackManagerForLLMRun | None = None,
    **kwargs: Any,
) -> ChatResult:
    """Replace only the provider call while retaining ChatOpenAI tool binding."""
    del model, stop, run_manager
    tool_names = {
        str(function.get("name", ""))
        for tool in kwargs.get("tools", [])
        if isinstance(tool, dict) and isinstance(function := tool.get("function"), dict)
    }
    return ChatResult(generations=[ChatGeneration(message=_response(messages, tool_names))])


def _response(messages: list[BaseMessage], tool_names: set[str]) -> AIMessage:
    prompt = _latest_prompt(messages)

    if "In which country is it?" in prompt:
        return AIMessage(content="France")

    if "What is the capital of France?" in prompt:
        return AIMessage(content="Paris")

    if "get_weather" in tool_names:
        cities = [city for city in CITIES if city in prompt]
        if not cities or _has_tool_returns(messages, {f"weather-{city.lower()}" for city in cities}):
            return AIMessage(content="Done.")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_weather",
                    "args": {"city": city},
                    "id": f"weather-{city.lower()}",
                    "type": "tool_call",
                }
                for city in cities
            ],
        )

    if "explode" in tool_names:
        return _tool_call("explode", {"reason": "scripted failure"}, "explode")

    if "ask_specialist" in tool_names:
        if _has_tool_returns(messages, {"ask-specialist"}):
            return AIMessage(content="Done.")
        return _tool_call(
            "ask_specialist",
            {"question": "How do I speed up a slow SQL query?"},
            "ask-specialist",
        )

    if "handoff_to_billing" in tool_names:
        if _has_tool_returns(messages, {"billing-handoff"}):
            return AIMessage(content="Done.")
        return _tool_call(
            "handoff_to_billing",
            {"question": "I was double charged on my invoice."},
            "billing-handoff",
        )

    return AIMessage(content="Done.")


def _latest_prompt(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message.content if isinstance(message.content, str) else str(message.content)
    return ""


def _has_tool_returns(messages: list[BaseMessage], call_ids: set[str]) -> bool:
    return any(isinstance(message, ToolMessage) and message.tool_call_id in call_ids for message in messages)


def _tool_call(name: str, args: dict[str, str], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )
