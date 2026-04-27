#
#  Copyright (c) 2023-2025 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""Restate integration for LangChain agents.

Pass `RestateMiddleware()` to `langchain.agents.create_agent(..., middleware=[...])`
and run the agent inside a Restate handler. Every LLM call is journaled by
Restate, so handler retries replay completed model calls from the journal
instead of re-executing them.

Use Restate context actions like `restate_context().run_typed("name", ...)`
inside the tool body for the side effects you want to be durable.
"""

import typing

from restate import Context, ObjectContext
from restate.server_context import current_context

from ._middleware import RestateMiddleware
from ._serde import PydanticTypeAdapter


def restate_context() -> Context:
    """Return the current Restate Context.

    Use this inside a tool body to wrap your side effects in
    `ctx.run_typed("name", ...)` — that's the explicit way to make them
    durable. The middleware does NOT auto-wrap tool calls.
    """
    ctx = current_context()
    if ctx is None:
        raise RuntimeError("No Restate context found.")
    return ctx


def restate_object_context() -> ObjectContext:
    """Return the current Restate ObjectContext. Errors if the agent is not
    running inside a Virtual Object handler."""
    ctx = current_context()
    if ctx is None:
        raise RuntimeError("No Restate context found.")
    return typing.cast(ObjectContext, ctx)


__all__ = [
    "RestateMiddleware",
    "restate_context",
    "restate_object_context",
]
