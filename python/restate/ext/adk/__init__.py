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
import typing

from .session import RestateSessionService
from .plugin import RestatePlugin
from restate import ObjectContext, Context
from restate.extensions import current_context


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
    "RestateSessionService",
    "RestatePlugin",
    "restate_object_context",
    "restate_context",
]
