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
A private link on the objects this package makes: set once, as it makes one,
and read by its code alone (a context's attempt, a database's attempt, a
transaction's reset, an object's entries). The object reads as its fields
alone, and its link goes with it. As sdk-sessions' ``link.ts``.
"""

from __future__ import annotations

import typing
import weakref

T = typing.TypeVar("T")
O = typing.TypeVar("O")


class Link(typing.Generic[T]):
    """A link of one kind, on the objects that have one: compared by identity, kept as long as the object is."""

    def __init__(self, what: str) -> None:
        self.what = what
        self._links: weakref.WeakKeyDictionary[object, T] = weakref.WeakKeyDictionary()

    def on(self, owner: O, value: T) -> O:
        """``owner``, linked to ``value``: once, as it is made."""
        self._links[owner] = value
        return owner

    def of(self, owner: object) -> T | None:
        """What ``owner`` is linked to, if it has this link (and None for anything that cannot have one)."""
        try:
            return self._links.get(owner)
        except TypeError:
            return None
