#
#  Copyright (c) 2023-2025 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""
A minimal OpenAI-agents service used by the AI integration tests.

It mirrors the shape of the `openai-agents` examples in restatedev/ai-examples:
a Virtual Object whose handler runs a tool-calling agent through Restate's
``DurableRunner`` with a durable session. This exercises the surfaces most
likely to break on an upstream ``openai-agents`` bump: LLM-call journaling,
durable tool execution and session-state (de)serialization across replay.
"""

import restate
from restate import ObjectContext

from agents import Agent
from restate.ext.openai import DurableRunner, durable_function_tool

# A cheap model keeps cost negligible while still producing genuinely
# non-deterministic output, which is exactly what surfaces replay bugs.
MODEL = "gpt-4o-mini"


@durable_function_tool
async def get_weather(city: str) -> str:
    """Get the current weather for a given city.

    Args:
        city: The city to get the weather for.
    """
    return f"The weather in {city} is sunny and 22 degrees Celsius."


agent = Agent(
    name="Assistant",
    instructions=(
        "You are a concise assistant. When the user asks about the weather you MUST "
        "call the get_weather tool. Answer in one short sentence."
    ),
    model=MODEL,
    tools=[get_weather],
)


agent_object = restate.VirtualObject("AgentObject")


@agent_object.handler()
async def message(ctx: ObjectContext, prompt: str) -> str:
    """Send a single message to the agent, persisting conversation history."""
    result = await DurableRunner.run(
        agent,
        prompt,
        use_restate_session=True,
    )
    return str(result.final_output)


def app():
    """Build the Restate ASGI app for this service."""
    return restate.app([agent_object])
