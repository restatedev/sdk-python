"""
A Claude agent on the Anthropic API: a weather assistant, each conversation in
its key's database, stored a step at a time. A crash mid-turn resumes from the
last stored step: a tool call already made is not made again::

    ANTHROPIC_API_KEY=... uv run --extra anthropic --extra serve examples-agents/basic/anthropic_agent.py
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080
    curl localhost:8080/assistant/alice/chat -d 'What is the weather in Lisbon?'
    curl localhost:8080/assistant/alice/messages --json '{}'                        # the conversation

The model is ``anthropic/claude-opus-5-5`` unless RESTATE_AGENT_MODEL names another
Claude model. On Amazon Bedrock, Vertex AI or Microsoft Foundry, pass that
platform's client (``client=``) and ``fallbacks=False``.
"""

import asyncio

import restate
from restate import sessions
from restate.agents import anthropic, tool


@tool
async def get_weather(city: str) -> str:
    """The current weather in a city."""
    return f"Sunny and 21°C in {city}"


assistant = anthropic.agent("assistant", system="Answer briefly.", tools=[get_weather])


if __name__ == "__main__":
    asyncio.run(restate.serve(sessions.app(assistant)))
