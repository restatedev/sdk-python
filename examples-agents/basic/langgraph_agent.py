"""
A LangGraph agent: a weather assistant, each conversation in its key's
database, stored a step at a time. A crash mid-turn resumes from the last
stored step: a tool call already made is not made again::

    OPENAI_API_KEY=... uv run --extra langgraph --extra serve --with langchain-openai examples-agents/basic/langgraph_agent.py
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080
    curl localhost:8080/langgraph_assistant/alice/chat -d 'What is the weather in Lisbon?'
    curl localhost:8080/langgraph_assistant/alice/messages --json '{}'              # the conversation

The model is ``openai/gpt-6-sol`` unless RESTATE_AGENT_MODEL names another:
``anthropic/<model-id>`` takes ``langchain-anthropic`` (and
ANTHROPIC_API_KEY) instead of ``langchain-openai``.
"""

import asyncio

import restate
from restate.agents import langgraph, tool


@tool
async def get_weather(city: str) -> str:
    """The current weather in a city."""
    return f"Sunny and 21°C in {city}"


assistant = langgraph.agent("langgraph_assistant", system="Answer briefly.", tools=[get_weather])


if __name__ == "__main__":
    asyncio.run(restate.serve(restate.app([assistant])))
