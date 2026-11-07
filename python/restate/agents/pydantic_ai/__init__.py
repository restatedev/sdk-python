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
"""Pydantic AI agents as session objects, with conversations stored in the object's database.

For example::

    import asyncio

    import restate
    from restate import sessions
    from restate.agents import pydantic_ai, tool

    @tool
    async def get_weather(city: str) -> str:
        '''Return the current weather in a city.'''
        return f"Sunny in {city}"

    assistant = pydantic_ai.agent("assistant", system="Answer briefly.", tools=[get_weather])
    asyncio.run(restate.serve(restate.app([assistant])))

``agent`` creates an agent, and ``prompt`` answers a prompt from a handler of
your own. The conversation is stored one step at a time.

This differs from the older ``restate.ext.pydantic`` integration, whose
``RestateAgent`` wraps a Pydantic AI ``Agent`` so that it runs durably inside
any regular Restate handler, journaling model calls and MCP tool calls (and,
optionally, function tools) with ``ctx.run_typed``. Pick
``restate.ext.pydantic`` to add durability to an agent you run in your own
services; pick this module for a ready-made agent per key, whose conversation
lives in a database and which comes with built-in handlers to chat, read the
conversation, watch its progress and stop a turn.

Install the ``pydantic_ai`` extra (``pip install restate-sdk[pydantic_ai]``).
Each model provider also needs Pydantic AI's extra for it, such as
``pydantic-ai-slim[openai]`` for ``openai/...`` models. Alternatively, pass
any Pydantic AI model as ``model``.
"""

try:
    import pydantic_ai  # noqa: F401  # pyright: ignore[reportUnusedImport]
except ImportError as error:
    raise ImportError(
        "restate.agents.pydantic_ai needs Pydantic AI: install the pydantic_ai extra, pip install restate-sdk[pydantic_ai]"
    ) from error

from restate.agents.pydantic_ai._agent import AnyCapability, AnyModel, AnyTool, agent, default_capabilities, prompt

__all__ = ["AnyCapability", "AnyModel", "AnyTool", "agent", "default_capabilities", "prompt"]
