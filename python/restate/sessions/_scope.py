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
"""
An attempt's scope: what its code keeps for the attempt (an agent's turn, the
calls of its tools...), which its context holds and which ends with it. A
retry gets a context, and so a scope, of its own. A module holds only the key
of what it keeps (``ScopeKey``), never the state. As sdk-sessions' ``scope.ts``.
"""

from __future__ import annotations

import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from restate.server_context import ServerInvocationContext
from restate.sessions._api import SessionContext, ViewContext
from restate.sessions._link import Link

T = typing.TypeVar("T")


@dataclass
class Scope:
    """An attempt's own, as this package made it, on its context (``SCOPE``), and what its code keeps for it."""

    sdk: ServerInvocationContext | None
    """The SDK's context of the attempt: none over a test's own host."""
    explicit_cancellation: bool = False
    """Whether its handler has ``explicit_cancellation=True``."""
    definition: object | None = None
    """The session object whose handler or view it runs (``definition_of``)."""
    values: dict[ScopeKey[Any], Any] = field(default_factory=dict[Any, Any])
    """What its code keeps for it, by key: only ``ScopeKey`` reaches it."""


SCOPE: Link[Scope] = Link("scope")
"""The attempt's scope, on its context."""


class ScopeKey(typing.Generic[T]):
    """
    A kind of state an attempt's code keeps, by its context: its key in the
    attempt's scope. Kept per attempt, so a retry starts without it::

        TURN: ScopeKey[Turn] = ScopeKey("an agent's turn")

        turn = TURN.of(ctx, lambda: Turn(...))
    """

    def __init__(self, what: str) -> None:
        self.what: str = what
        """What it is, for an error."""

    def get(self, ctx: SessionContext | ViewContext) -> T | None:
        """What the attempt of ``ctx`` keeps under this key, if anything."""
        return typing.cast("T | None", self._values(ctx).get(self))

    def set(self, ctx: SessionContext | ViewContext, value: T) -> None:
        """Keep ``value`` under this key, for the attempt of ``ctx``."""
        self._values(ctx)[self] = value

    def delete(self, ctx: SessionContext | ViewContext) -> None:
        """Keep nothing under this key, for the attempt of ``ctx``."""
        self._values(ctx).pop(self, None)

    def of(self, ctx: SessionContext | ViewContext, make: Callable[[], T]) -> T:
        """What the attempt of ``ctx`` keeps under this key, made by ``make`` the first time."""
        values = self._values(ctx)
        if self not in values:
            values[self] = make()
        return typing.cast(T, values[self])

    def _values(self, ctx: SessionContext | ViewContext) -> dict[ScopeKey[Any], Any]:
        scope = SCOPE.of(ctx)
        if scope is None:
            raise TypeError(f"{self.what} takes the context of a session object's handler or view")
        return scope.values
