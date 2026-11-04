#
#  Copyright (c) 2023-2026 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""OpenAI Agents SDK agents as session objects, with conversations stored in the object's database.

For example::

    import asyncio

    import restate
    from restate import sessions
    from restate.agents import openai, tool

    @tool
    async def get_weather(city: str) -> str:
        '''Return the current weather in a city.'''
        return f"Sunny in {city}"

    assistant = openai.agent("assistant", system="Answer briefly.", tools=[get_weather])
    asyncio.run(restate.serve(sessions.app(assistant)))

``agent`` creates an agent, and ``prompt`` answers a prompt from a handler of
your own; either takes an ``Agent`` of your own, with handoffs and guardrails.
``tools`` gives the agents it hands off to this package's tools. The
conversation is stored one step at a time.

This differs from the older ``restate.ext.openai`` integration, whose
``DurableRunner`` runs an agent durably inside any regular Restate handler,
journaling its model calls with ``ctx.run_typed``. Pick ``restate.ext.openai`` to
add durability to an agent you run in your own services; pick this module for
a ready-made agent per key, whose conversation lives in a database and which
comes with built-in handlers to chat, read the conversation, watch its
progress and stop a turn.

Install the ``openai`` extra (``pip install restate-sdk[openai]``). Requests
use OPENAI_API_KEY, unless you pass a model of the SDK of your own.
"""

try:
    import agents  # noqa: F401  # pyright: ignore[reportUnusedImport]
except ImportError as error:
    raise ImportError(
        "restate.agents.openai needs the OpenAI Agents SDK: install the openai extra, pip install restate-sdk[openai]"
    ) from error

from restate.agents.openai._agent import AnyModel, AnyTool, agent, prompt, tools

__all__ = ["AnyModel", "AnyTool", "agent", "prompt", "tools"]
