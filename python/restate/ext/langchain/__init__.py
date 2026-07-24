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

Pass `RestateMiddleware()` to `create_agent(..., middleware=[...])` and run
the agent inside a Restate handler. LLM responses are journaled, so retries
replay them instead of re-calling the model. To make tool side effects
durable, wrap them with `restate_context().run_typed("name", ...)` inside
the tool body.
"""

import os
import typing

from restate import Context, ObjectContext
from restate.server_context import current_context

from ._middleware import RestateMiddleware
from ._stategraph import durable_scope, enable


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
    "durable_scope",
    "enable",
]


# Zero-config: importing the LangGraph integration is enough. Parallel StateGraph
# nodes journal deterministically with NO user code — no wrappers, no startup call.
# Set RESTATE_STATEGRAPH_DETERMINISM=0 to opt out. Never let this break the import.
if os.environ.get("RESTATE_STATEGRAPH_DETERMINISM", "1") != "0":
    try:
        enable()
    except Exception:  # pragma: no cover  pylint: disable=broad-except
        pass
