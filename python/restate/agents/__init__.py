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
"""AI agents built on session objects, with each conversation kept in its key's database.

A crash, a restart, or a person who takes a day to approve something changes
nothing: each message is answered once, and each tool call is made once::

    import asyncio

    import restate
    from restate import sessions
    from restate.agents import langgraph, tool


    @tool
    async def get_weather(city: str) -> str:
        '''The current weather in a city.'''
        return f"Sunny in {city}"


    assistant = langgraph.agent("assistant", system="Answer briefly.", tools=[get_weather])

    if __name__ == "__main__":
        asyncio.run(restate.serve(restate.app([assistant])))

An agent answers with text, or with data: give ``output`` a Pydantic model,
and ``chat`` (and each framework's ``prompt``) answers with a value of it::

    planner = langgraph.agent("planner", system="Plan the trip.", output=Plan)

Each framework's ``agent`` and ``prompt`` take either the common options of a
built-in agent (``model``, ``system``, ``tools``), or the framework's own
agent, configured as the framework does, for everything else: an ``agent=``
(Pydantic AI, OpenAI Agents SDK, ADK) or a ``graph=`` (LangGraph)::

    desk = pydantic_ai.agent("desk", agent=Agent(model, capabilities=[...], toolsets=[pydantic_ai.toolset([get_weather])]))

This package holds what all frameworks share:

- ``AgentObject``, a session object set up for agents.
- Tools that work with any framework: ``tool`` for your own functions, and
  ``call``, ``send``, ``workflow``, ``subagent`` and ``wait_for_signal``,
  which are built on Restate.
- ``PerSession``, for agent options that differ per invocation.
- ``webhook``, which turns another system's webhooks into messages to agents.
- The types of the conversation that clients read and send.

Each framework has a subpackage and an extra of its own:
``restate.agents.langgraph`` (``pip install "restate-sdk[langgraph]"``),
``restate.agents.pydantic_ai`` (``pip install "restate-sdk[pydantic_ai]"``),
``restate.agents.openai``, on the OpenAI Agents SDK (``pip install "restate-sdk[openai]"``) and
``restate.agents.adk``, on Google ADK (``pip install "restate-sdk[adk]"``).
``restate.agents.internal`` has the building blocks for writing another
integration.

``restate.agents.client`` talks to an agent from any Python code (send
messages, follow the conversation, resolve waits), and
``python -m restate.agents chat assistant/alice`` from a terminal; both need
the ``client`` extra (``pip install "restate-sdk[client]"``).
"""

from restate.agents._api import (
    ChatMessage,
    Messages,
    MessagesRequest,
    Notifications,
    Part,
    Resolved,
    Signal,
    Status,
    Stopped,
    TextPart,
    ToolPart,
    Turn,
    Waiting,
    Watch,
)
from restate.agents._common import PerSession
from restate.agents._object import AgentObject, AgentOptions, Synced, SyncRequest
from restate.agents._tools import Tool, ToolCall, call, send, subagent, tool, wait_for_signal, workflow
from restate.agents._webhook import Event, webhook, webhook_message

__all__ = [
    "AgentObject",
    "AgentOptions",
    "ChatMessage",
    "Event",
    "Messages",
    "MessagesRequest",
    "Notifications",
    "Part",
    "PerSession",
    "Resolved",
    "Signal",
    "Status",
    "Stopped",
    "SyncRequest",
    "Synced",
    "TextPart",
    "Tool",
    "ToolCall",
    "ToolPart",
    "Turn",
    "Waiting",
    "Watch",
    "call",
    "send",
    "subagent",
    "tool",
    "wait_for_signal",
    "webhook",
    "webhook_message",
    "workflow",
]
