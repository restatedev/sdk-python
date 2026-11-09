"""
An OpenAI Agents SDK agent: a weather assistant, each conversation in its
key's database, stored a step at a time. A crash mid-turn resumes from the
last stored step: a tool call already made is not made again::

    OPENAI_API_KEY=... uv run --extra openai --extra serve examples-agents/basic/openai_agent.py
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080
    curl localhost:8080/openai_assistant/alice/chat -d 'What is the weather in Lisbon?'
    curl localhost:8080/openai_assistant/alice/messages --json '{}'                 # the conversation

The model is ``openai/gpt-6-sol`` unless RESTATE_AGENT_MODEL names another
OpenAI model. To bring an ``Agent`` of your own, with handoffs and guardrails,
pass it as ``openai.agent("openai_assistant", agent=Agent(...))``.
"""

import asyncio

import restate
from restate.agents import openai, tool


@tool
async def get_weather(city: str) -> str:
    """The current weather in a city."""
    return f"Sunny and 21°C in {city}"


assistant = openai.agent("openai_assistant", system="Answer briefly.", tools=[get_weather])


if __name__ == "__main__":
    asyncio.run(restate.serve(restate.app([assistant])))
