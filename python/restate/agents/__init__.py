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
        asyncio.run(restate.serve(sessions.app(assistant)))

This package holds what all frameworks share:

- ``AgentObject``, a session object set up for agents.
- Tools that work with any framework: ``tool`` for your own functions, and
  ``call``, ``send``, ``workflow``, ``subagent`` and ``wait_for_signal``,
  which are built on Restate.
- ``PerSession``, for agent options that differ per invocation.
- The types of the conversation that clients read and send.

Each framework has a subpackage and an extra of its own:
``restate.agents.langgraph`` (``pip install "restate-sdk[langgraph]"``),
``restate.agents.pydantic_ai`` (``pip install "restate-sdk[pydantic_ai]"``) and
``restate.agents.openai``, on the OpenAI Agents SDK (``pip install "restate-sdk[openai]"``).
``restate.agents.internal`` has the building blocks for writing another
integration.
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
from restate.agents._object import AgentObject, Synced, SyncRequest
from restate.agents._tools import Tool, ToolCall, call, send, subagent, tool, wait_for_signal, workflow

__all__ = [
    "AgentObject",
    "ChatMessage",
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
    "workflow",
]
