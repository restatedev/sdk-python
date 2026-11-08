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
your own. Either takes the common options of a built-in agent (``model``,
``system``, ``tools``), or an ``Agent`` of your own, configured as Pydantic
AI does (capabilities, instructions functions, history processors...), with
its runs' ``deps`` and ``usage_limits``::

    mine = Agent("openai:gpt-6-sol", capabilities=[...], toolsets=[pydantic_ai.toolset([get_weather])])
    desk = pydantic_ai.agent("desk", agent=mine)

``toolset`` gives an ``Agent`` of your own this package's tools. The
conversation is stored one step at a time, and the model gets it since the
latest compaction, whole: Pydantic AI Harness's ``TieredCompaction``
(``compaction``) unless your ``Agent`` has a compaction of its own. A tool
call that needs an approval (``requires_approval``) or a result from outside
waits for a signal (``APPROVAL``, or the tool's name). ``plan_store``,
``overflow_store`` and ``history_source`` give the harness's ``Planning``,
``ToolOutputLimits`` and ``ConversationSearch`` the key's database::

    mine = Agent(model, capabilities=[Planning(store=pydantic_ai.plan_store())])

This differs from the older ``restate.ext.pydantic`` integration, whose
``RestateAgent`` wraps a Pydantic AI ``Agent`` so that it runs durably inside
any regular Restate handler, journaling model calls and MCP tool calls (and,
optionally, function tools) with ``ctx.run_typed``. Pick
``restate.ext.pydantic`` to add durability to an agent you run in your own
services; pick this module for a ready-made agent per key, whose conversation
lives in a database and which comes with built-in handlers to chat, read the
conversation, watch its progress and stop a turn.

Install the ``pydantic_ai`` extra (``pip install restate-sdk[pydantic_ai]``),
which brings Pydantic AI Harness.
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

from restate.agents.pydantic_ai._agent import (
    APPROVAL,
    AnyModel,
    AnyTool,
    agent,
    approval_of,
    compaction,
    history_source,
    prompt,
    toolset,
)
from restate.agents.pydantic_ai._stores import overflow_store, plan_store

__all__ = [
    "APPROVAL",
    "AnyModel",
    "AnyTool",
    "agent",
    "approval_of",
    "compaction",
    "history_source",
    "overflow_store",
    "plan_store",
    "prompt",
    "toolset",
]
