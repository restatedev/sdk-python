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
"""LangGraph's own errors that a retry would meet again, which end a turn for good."""

from __future__ import annotations

from langgraph.errors import EmptyInputError, GraphRecursionError, InvalidUpdateError

from restate.exceptions import TerminalError


def terminal_of(error: BaseException) -> TerminalError | None:
    """Return the terminal error that ends the turn whose graph run failed with ``error``, if a retry cannot help.

    That is so for the errors that LangGraph's runtime raises itself (its
    public ``langgraph.errors``), rather than a node, a tool or a model:

    - ``GraphRecursionError``: the run took more steps than its
      ``recursion_limit`` without finishing (a model that keeps calling
      tools, say). A retry resumes the run with a fresh limit, from where it
      stopped, so it would run on, and call the model again, at each retry.
    - ``InvalidUpdateError``: a step's update does not fit the state (two
      nodes writing a field that takes one value per step, say). A retry
      runs the step again from the same checkpoint, to the same update.
    - ``EmptyInputError``: the run had no input.

    Python's own ``RecursionError`` is not one of them: user code may raise it.
    """
    if isinstance(error, GraphRecursionError):
        why = "the graph stopped at its recursion limit"
    elif isinstance(error, InvalidUpdateError):
        why = "the graph made an invalid state update"
    elif isinstance(error, EmptyInputError):
        why = "the graph got no input"
    else:
        return None
    return TerminalError(f"{why}: {error}")
