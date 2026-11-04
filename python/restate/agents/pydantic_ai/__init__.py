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
"""
Pydantic AI agents in session objects: each conversation in the object's
database, stored a step at a time::

    from restate import sessions
    from restate.agents import pydantic_ai, tool

    @tool
    async def get_weather(city: str) -> str:
        '''The current weather in a city.'''
        return f"Sunny in {city}"

    assistant = pydantic_ai.agent("assistant", system="Answer briefly.", tools=[get_weather])
    sessions.serve(assistant)

``agent`` creates an agent; ``prompt`` answers a prompt in a handler of your own.

Install the ``pydantic_ai`` extra (``pip install restate-sdk[pydantic_ai]``).
A model's provider also needs Pydantic AI's extra for it, such as
``pydantic-ai-slim[openai]`` for ``openai/...`` models; or pass any Pydantic
AI model as ``model``.
"""

try:
    import pydantic_ai  # noqa: F401  # pyright: ignore[reportUnusedImport]
except ImportError as error:
    raise ImportError(
        "restate.agents.pydantic_ai needs Pydantic AI: install the pydantic_ai extra, pip install restate-sdk[pydantic_ai]"
    ) from error

from restate.agents.pydantic_ai._agent import AnyModel, AnyTool, agent, prompt

__all__ = ["AnyModel", "AnyTool", "agent", "prompt"]
