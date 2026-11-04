"""
A Pydantic AI agent: a weather assistant, each conversation in its key's
database, stored a step at a time. A crash mid-turn resumes from the last
stored step: a tool call already made is not made again::

    OPENAI_API_KEY=... uv run --extra pydantic_ai --extra serve --with 'pydantic-ai-slim[openai]' examples-agents/basic/pydantic_ai_agent.py
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080
    curl localhost:8080/assistant/alice/chat -d 'What is the weather in Lisbon?'
    curl localhost:8080/assistant/alice/messages --json '{}'                        # the conversation

The model is ``openai/gpt-6-sol`` unless RESTATE_AGENT_MODEL names another, of any of
Pydantic AI's providers: ``anthropic/<model-id>`` takes
``pydantic-ai-slim[anthropic]`` (and ANTHROPIC_API_KEY) instead.
"""

import asyncio

import restate
from restate import sessions
from restate.agents import pydantic_ai, tool


@tool
async def get_weather(city: str) -> str:
    """The current weather in a city."""
    return f"Sunny and 21°C in {city}"


assistant = pydantic_ai.agent("assistant", system="Answer briefly.", tools=[get_weather])


if __name__ == "__main__":
    asyncio.run(restate.serve(sessions.app(assistant)))
