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
"""Google ADK agents as session objects, with their sessions stored in the object's database.

For example::

    import asyncio

    import restate
    from restate import sessions
    from restate.agents import adk, tool

    @tool
    async def get_weather(city: str) -> str:
        '''Return the current weather in a city.'''
        return f"Sunny in {city}"

    assistant = adk.agent("assistant", model="google/<model-id>", system="Answer briefly.", tools=[get_weather])
    asyncio.run(restate.serve(restate.app([assistant])))

``agent`` creates an agent, and ``prompt`` answers a prompt from a handler of
your own; either takes an agent of your own (with sub-agents and callbacks)
or an ``App`` (with plugins). ``tools`` gives its sub-agents this package's
tools. ADK runs the agent as a resumable app, on a session whose events are
stored one at a time.

This differs from the older ``restate.ext.adk`` integration, whose plugin
runs an agent durably inside any regular Restate handler, journaling its
model calls with ``ctx.run_typed``. Pick ``restate.ext.adk`` to add
durability to an agent you run in your own services; pick this module for a
ready-made agent per key, whose conversation lives in a database and which
comes with built-in handlers to chat, read the conversation, watch its
progress and stop a turn.

Install the ``adk`` extra (``pip install restate-sdk[adk]``). Gemini models
use GOOGLE_API_KEY; other providers take an ADK model of your own, such as
``LiteLlm(...)``.
"""

try:
    import google.adk  # noqa: F401  # pyright: ignore[reportUnusedImport]
except ImportError as error:
    raise ImportError(
        "restate.agents.adk needs Google ADK: install the adk extra, pip install restate-sdk[adk]"
    ) from error

from restate.agents.adk._agent import AnyModel, AnyTool, agent, prompt, tools

__all__ = ["AnyModel", "AnyTool", "agent", "prompt", "tools"]
