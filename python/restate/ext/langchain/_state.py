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

from typing import Optional

from restate import Context
from restate.ext.turnstile import Turnstile
from restate.server_context import get_extension_data, set_extension_data


def _extension_key(invocation_id: str) -> str:
    return "restate_langchain_" + invocation_id


class _State:
    """Per-handler middleware state held on the Restate ``Context``.

    Stored in ``ctx.extension_data`` (mirrors ``restate.ext.adk``'s
    ``PluginState``) so every asyncio task spawned during the handler
    invocation reaches the same instance via the shared ``Context``
    object — independent of ``ContextVar`` inheritance. A ``ContextVar``
    binding set deep inside an ``awrap_model_call`` task didn't reach
    the sibling ``awrap_tool_call`` tasks spawned by langgraph's
    ``tool_node``; ``ctx.extension_data`` does.

    Holds a map of ``tool_call_id -> Turnstile``. Each model turn creates one
    turnstile and registers it under every tool-call id in that batch, so
    ``awrap_tool_call`` finds it by its own id. Keying by the (globally unique)
    tool-call id — rather than a single shared slot — is what makes this safe
    when multiple agents run concurrently in parallel LangGraph nodes: sibling
    branches write to distinct keys and never clobber each other.
    """

    __slots__ = ("turnstiles",)

    def __init__(self) -> None:
        self.turnstiles: dict[str, Turnstile] = {}

    def __close__(self) -> None:
        # Called at handler end via auto_close_extension_data. Cancel each
        # distinct turnstile once (ids in a batch share one instance).
        seen: set[int] = set()
        for t in self.turnstiles.values():
            if id(t) not in seen:
                seen.add(id(t))
                t.cancel_all()


def get_or_create_state(ctx: Context) -> _State:
    state: Optional[_State] = get_extension_data(ctx, "langchain-state")
    if state is None:
        state = _State()
        set_extension_data(ctx, "langchain-state", state)
    return state


def state_from_ctx(ctx: Context) -> Optional[_State]:
    return get_extension_data(ctx, "langchain-state")
