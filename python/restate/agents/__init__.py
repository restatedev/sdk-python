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
"""AI agents in session objects, each conversation kept in its key's database.

A crash, a restart, or a person who takes a day to approve changes nothing:
each message is answered once, and each tool call is made once::

    from restate import sessions
    from restate.agents import langgraph, tool


    @tool
    async def get_weather(city: str) -> str:
        '''The current weather in a city.'''
        return f"Sunny in {city}"


    assistant = langgraph.agent("assistant", system="Answer briefly.", tools=[get_weather])

    if __name__ == "__main__":
        sessions.serve(assistant)

This package has what every framework shares: ``AgentObject``, session
objects set up for agents; tools for any framework (``tool``, and the tools
that are Restate itself: ``call``, ``send``, ``workflow``, ``subagent``,
``wait_for_signal``); and the types of the conversation that clients read.
Each framework is a subpackage with an extra of its own:
``restate.agents.langgraph`` and ``restate.agents.pydantic_ai``. What
integrations need beside this is in ``restate.agents.internal``.
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
