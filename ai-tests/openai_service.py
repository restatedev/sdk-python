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
"""
import uuid
import restate
from restate import ObjectContext

from agents import Agent
from restate.ext.openai import DurableRunner, durable_function_tool, restate_object_context

# A cheap model keeps cost negligible while still producing genuinely
# non-deterministic output, which is exactly what surfaces replay bugs.
MODEL = "gpt-4o-mini"


@durable_function_tool
async def get_weather(city: str) -> str:
    """Get the current weather for a given city.

    Args:
        city: The city to get the weather for.
    """
    async def call_weather_api():
        return f"The weather in {str(uuid.uuid4())} is sunny and 22 degrees Celsius."
    return await restate_object_context().run_typed("call weather api", call_weather_api)


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
