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
"""LangGraph agents as session objects, with each conversation stored in its key's database.

For example::

    import asyncio

    import restate
    from restate import sessions
    from restate.agents import langgraph, tool

    @tool
    async def get_weather(city: str) -> str:
        '''Return the current weather in a city.'''
        return f"Sunny in {city}"

    assistant = langgraph.agent("assistant", system="Answer briefly.", tools=[get_weather])
    asyncio.run(restate.serve(restate.app([assistant])))

``agent`` creates an agent, ``prompt`` answers a prompt from a handler of your
own, and ``SqlCheckpointSaver`` stores the checkpoints of a graph of your own
in ``ctx.db``. ``nodes`` has prebuilt nodes for such a graph that are Restate
steps: wait for a signal, run a workflow, ask another agent.

This differs from the older ``restate.ext.langchain`` integration, whose
middleware makes a LangChain ``create_agent`` agent durable inside any regular
Restate handler by journaling each model response, and leaves tool side
effects for you to wrap in ``ctx.run_typed``. Pick ``restate.ext.langchain``
to add durability to an agent you run in your own services; pick this module
for a ready-made agent per key, whose conversation lives in a database and
which comes with built-in handlers to chat, read the conversation, watch its
progress and stop a turn.

Install the ``langgraph`` extra (``pip install restate-sdk[langgraph]``). A
model named ``openai/...`` also needs ``langchain-openai``, and one named
``anthropic/...`` needs ``langchain-anthropic``. Alternatively, pass any
LangChain chat model as ``model``.
"""

try:
    import langchain_core  # noqa: F401  # pyright: ignore[reportUnusedImport]

    import langgraph  # noqa: F401  # pyright: ignore[reportUnusedImport]
except ImportError as error:
    raise ImportError(
        "restate.agents.langgraph needs LangGraph: install the langgraph extra, pip install restate-sdk[langgraph]"
    ) from error

from restate.agents.langgraph._agent import AnyTool, ChatModel, agent, prompt
from restate.agents.langgraph._checkpointer import SqlCheckpointSaver

__all__ = ["AnyTool", "ChatModel", "SqlCheckpointSaver", "agent", "prompt"]
