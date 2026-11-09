"""
A Google ADK agent: a weather assistant, each conversation an ADK session in
its key's database, stored an event at a time. A crash mid-turn resumes from
the last stored event: a tool call already made is not made again::

    GOOGLE_API_KEY=... RESTATE_AGENT_MODEL=google/<model-id> uv run --extra adk --extra serve examples-agents/basic/adk_agent.py
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080
    curl localhost:8080/adk_assistant/alice/chat -d 'What is the weather in Lisbon?'
    curl localhost:8080/adk_assistant/alice/messages --json '{}'                    # the conversation

The model is RESTATE_AGENT_MODEL, a Gemini model (``google/<model-id>``). For
another provider, pass an ADK model of your own: ``model=LiteLlm(...)``.
"""

import asyncio

import restate
from restate.agents import adk, tool


@tool
async def get_weather(city: str) -> str:
    """The current weather in a city."""
    return f"Sunny and 21°C in {city}"


assistant = adk.agent("adk_assistant", system="Answer briefly.", tools=[get_weather])


if __name__ == "__main__":
    asyncio.run(restate.serve(restate.app([assistant])))
