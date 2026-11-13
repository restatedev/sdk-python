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
"""The handler's context of a graph's run, for this package's prebuilt nodes and Restate tools.

``langgraph.agent(graph=...)`` sets it for each run, which leaves LangGraph's
run context (``runtime.context``) to the graph: one with a ``context_schema``
of its own gets its own. A graph that a handler of your own runs, with
``ainvoke(..., context=ctx)``, has the handler's context as its run context,
which the nodes and tools fall back to.
"""

from __future__ import annotations

import contextlib
import contextvars
import typing
from collections.abc import Generator

from restate.sessions import SessionContext

_RUN: contextvars.ContextVar[SessionContext | None] = contextvars.ContextVar("restate_langgraph_run", default=None)


@contextlib.contextmanager
def running(ctx: SessionContext) -> Generator[None, None, None]:
    """Run a graph for the handler of ``ctx``: its nodes and tools find it with ``handler_context``."""
    token = _RUN.set(ctx)
    try:
        yield
    finally:
        _RUN.reset(token)


def handler_context(run_context: object) -> SessionContext | None:
    """Return the handler's context of the graph run in progress, given LangGraph's run context.

    That is the one ``running`` set, else the run context itself, for a
    graph invoked with ``context=ctx``; ``None`` if there is neither.
    """
    ctx = _RUN.get()
    return ctx if ctx is not None else typing.cast("SessionContext | None", run_context)
