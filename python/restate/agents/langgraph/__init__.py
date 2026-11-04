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
LangGraph agents in session objects: a tool-calling graph whose checkpoints
are in the object's database. As sdk-sessions' ``@restatedev/agents/langgraph``::

    from restate import sessions
    from restate.agents import langgraph, tool

    @tool
    async def get_weather(city: str) -> str:
        '''The current weather in a city.'''
        return f"Sunny in {city}"

    assistant = langgraph.agent("assistant", system="Answer briefly.", tools=[get_weather])
    sessions.serve(assistant)

It needs the ``langgraph`` extra (``pip install restate-sdk[langgraph]``);
a model named ``openai/...`` needs ``langchain-openai`` beside it, and
``anthropic/...`` ``langchain-anthropic``. Or bring a LangChain chat model of
your own (``model=``). ``agent`` makes an agent; ``chat`` is the turn of an
``AgentObject`` of your own, ``prompt`` a prompt of a handler of your own,
and ``SqlCheckpointSaver`` the checkpointer, for a graph of your own.
"""

try:
    import langchain_core  # noqa: F401  # pyright: ignore[reportUnusedImport]

    import langgraph  # noqa: F401  # pyright: ignore[reportUnusedImport]
except ImportError as error:
    raise ImportError(
        "restate.agents.langgraph needs LangGraph: install the langgraph extra, pip install restate-sdk[langgraph]"
    ) from error

from restate.agents.langgraph._agent import Options, agent, chat, prompt
from restate.agents.langgraph._checkpointer import SqlCheckpointSaver

__all__ = ["Options", "SqlCheckpointSaver", "agent", "chat", "prompt"]
