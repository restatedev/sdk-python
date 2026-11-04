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
"""Claude agents in session objects, on the Anthropic API.

Each conversation is kept in the object's database, a step at a time::

    import asyncio

    import restate
    from restate import sessions
    from restate.agents import anthropic, tool

    @tool
    async def get_weather(city: str) -> str:
        '''The current weather in a city.'''
        return f"Sunny in {city}"

    assistant = anthropic.agent("assistant", system="Answer briefly.", tools=[get_weather])
    asyncio.run(restate.serve(sessions.app(assistant)))

``agent`` creates an agent; ``prompt`` answers a prompt in a handler of your own.

The agent runs Claude's tool-calling loop on the official ``anthropic`` SDK,
which is lighter than the Claude Agent SDK (Claude Code as a subprocess). It
defaults to ``claude-opus-5-5``, asks the API to fall back to another model
when a request is declined (``fallbacks=True``; turn it off on Amazon Bedrock,
Vertex AI or Microsoft Foundry), and caches the conversation's prefix.

Install the ``anthropic`` extra (``pip install restate-sdk[anthropic]``).
Credentials come from the environment (``ANTHROPIC_API_KEY``, or an
``ant auth login`` profile), or from a ``client`` of your own.
"""

try:
    import anthropic  # noqa: F401  # pyright: ignore[reportUnusedImport]
except ImportError as error:
    raise ImportError(
        "restate.agents.anthropic needs the Anthropic SDK: install the anthropic extra, "
        "pip install restate-sdk[anthropic]"
    ) from error

from restate.agents.anthropic._agent import AnyTool, Effort, agent, prompt

__all__ = ["AnyTool", "Effort", "agent", "prompt"]
