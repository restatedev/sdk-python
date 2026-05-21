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

from contextvars import ContextVar

from restate.ext.turnstile import Turnstile


class _State:
    __slots__ = ("turnstile",)

    def __init__(self) -> None:
        self.turnstile: Turnstile = Turnstile([])


# No default: a module-level default `_State()` would be a single object shared
# across every asyncio task that never calls `_state_var.set(...)`.
_state_var: ContextVar[_State] = ContextVar("restate_langchain_state")


def current_state() -> _State:
    try:
        return _state_var.get()
    except LookupError:
        state = _State()
        _state_var.set(state)
        return state
