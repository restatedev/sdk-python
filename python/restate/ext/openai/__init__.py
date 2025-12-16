#
#  Copyright (c) 2023-2024 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""
This module contains the optional OpenAI integration for Restate.
"""

import typing

from restate import ObjectContext, Context
from restate.server_context import current_context

from .runner_wrapper import DurableRunner
from .models import LlmRetryOpts
from .functions import continue_on_terminal_errors, raise_terminal_errors, function_tool


def restate_object_context() -> ObjectContext:
    """Get the current Restate ObjectContext."""
    ctx = current_context()
    if ctx is None:
        raise RuntimeError("No Restate context found.")
    return typing.cast(ObjectContext, ctx)


def restate_context() -> Context:
    """Get the current Restate Context."""
    ctx = current_context()
    if ctx is None:
        raise RuntimeError("No Restate context found.")
    return ctx


__all__ = [
    "DurableRunner",
    "LlmRetryOpts",
    "restate_object_context",
    "restate_context",
    "continue_on_terminal_errors",
    "raise_terminal_errors",
    "function_tool",
]
