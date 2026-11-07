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
steps: wait for a signal, run a workflow, call or send to a handler, ask another
agent.

Or bring a graph of your own, such as LangChain's ``create_agent`` makes, with
this package's tools::

    graph = create_agent(model, tools=langgraph.tools([call(order_status, description="An order's status.")]))
    desk = langgraph.agent("desk", graph=graph)

Its ``interrupt()`` calls wait for a signal (``"interrupt"``), which the
agent's ``pending`` view lists and its ``signal`` view resolves.

Good to know about a graph of your own:

- A graph is durable a step at a time: a crash runs the step it cut off
  again. That is fine for a model request, but a step's other side effects
  (a payment, a plain LangChain tool) happen again. Make them Restate's: a
  prebuilt node, or a Restate tool from ``tools()`` (``call``, ``send``,
  ``workflow``, ``subagent``, ``wait_for_signal``), does its step once.
- A run that waits (for a signal, say) is checkpointed with the graph as it
  was. Changing the graph meanwhile (renaming a node, or a state field) is a
  migration: keep what waiting runs need until they are done, as with a
  database's tables.
- LangGraph stores a channel's whole value at each step that changes it, so a
  long conversation stores its messages again at each step. Trim the history
  (a summarizing node, say) in graphs that live long.
- Clients see a run step by step, as each step is stored: there is no
  token streaming.
- Pause a graph with ``interrupt()``, not static breakpoints
  (``interrupt_before``, ``interrupt_after``), which have nothing to wait
  for: a graph with them is refused.
- A graph with a ``context_schema`` (LangGraph's run context) gets it from
  ``langgraph.agent(..., context=...)``, a value or a function of the
  handler's context. The prebuilt nodes and Restate tools get the handler's
  context either way.
- Only an error of the graph's own chat models (LangChain's) can be its
  provider refusing (a wrong API key, say): the turn ends unanswered. Any
  other error fails the attempt, and Restate retries it: a tool's (a model
  that a tool calls included), or a node's, such as one that calls a
  provider's SDK itself.

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

from restate.agents.langgraph._agent import AnyTool, ChatModel, agent, prompt, tools
from restate.agents.langgraph._checkpointer import SqlCheckpointSaver
from restate.agents.langgraph._graph import AnyGraph

__all__ = ["AnyGraph", "AnyTool", "ChatModel", "SqlCheckpointSaver", "agent", "prompt", "tools"]
