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
"""Per-attempt state, kept on the attempt's context.

Code running in an attempt can keep state for the duration of that attempt
(an agent's turn, the calls its tools made, ...). The state lives on the
context and ends with it; a retry gets a new context and starts empty.
Modules hold only a ``ScopeKey``, never the state itself.
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
    """The per-attempt data this package attaches to a context (see ``SCOPE``)."""

    sdk: ServerInvocationContext | None
    """The SDK's context of the attempt, or ``None`` when running on a test's host."""
    explicit_cancellation: bool = False
    """Whether its handler has ``explicit_cancellation=True``."""
    definition: object | None = None
    """The session object whose handler or view the attempt runs (see ``definition_of``)."""
    values: dict[ScopeKey[Any], Any] = field(default_factory=dict[Any, Any])
    """The state kept for the attempt, by key; only ``ScopeKey`` accesses it."""


SCOPE: Link[Scope] = Link("scope")
"""The attempt's scope, attached to its context."""


class ScopeKey(typing.Generic[T]):
    """A key for one kind of state kept per attempt, on the attempt's context.

    The state is not carried over to a retry, which starts without it::

        TURN: ScopeKey[Turn] = ScopeKey("an agent's turn")

        turn = TURN.of(ctx, lambda: Turn(...))

    Args:
        what: A description of the state, used in error messages.
    """

    def __init__(self, what: str) -> None:
        self.what: str = what
        """A description of the state, used in error messages."""

    def get(self, ctx: SessionContext | ViewContext) -> T | None:
        """Return the value kept under this key for the attempt of ``ctx``, or ``None``."""
        return typing.cast("T | None", self._values(ctx).get(self))

    def set(self, ctx: SessionContext | ViewContext, value: T) -> None:
        """Keep ``value`` under this key for the attempt of ``ctx``."""
        self._values(ctx)[self] = value

    def delete(self, ctx: SessionContext | ViewContext) -> None:
        """Remove the value kept under this key for the attempt of ``ctx``, if any."""
        self._values(ctx).pop(self, None)

    def of(self, ctx: SessionContext | ViewContext, make: Callable[[], T]) -> T:
        """Return the value kept under this key for the attempt of ``ctx``, creating it with ``make`` if absent.

        Raises:
            TypeError: If ``ctx`` is not the context of a session object's
                handler or view.
        """
        values = self._values(ctx)
        if self not in values:
            values[self] = make()
        return typing.cast(T, values[self])

    def _values(self, ctx: SessionContext | ViewContext) -> dict[ScopeKey[Any], Any]:
        scope = SCOPE.of(ctx)
        if scope is None:
            raise TypeError(f"{self.what} takes the context of a session object's handler or view")
        return scope.values
