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
"""Private data attached to objects that this package creates.

A ``Link`` attaches a value to an object (a context's attempt, a database's
attempt, a transaction's reset, an object's entries) without adding a field
to it. The value is set once, when the object is created, is read only by
this package, and is dropped together with the object.
"""

from __future__ import annotations

import typing
import weakref

T = typing.TypeVar("T")
O = typing.TypeVar("O")


class Link(typing.Generic[T]):
    """A mapping from objects, by identity, to one kind of attached value.

    Entries live as long as their owner. A value that refers back to its
    owner (an attempt that refers to its database, say) would keep both alive
    forever; pass ``weak=True`` to hold values weakly, so that the owner must
    keep the value alive itself.
    """

    def __init__(self, what: str, *, weak: bool = False) -> None:
        self.what: str = what
        self._weak: bool = weak
        self._links: weakref.WeakKeyDictionary[object, T | weakref.ref[typing.Any]] = weakref.WeakKeyDictionary()

    def on(self, owner: O, value: T) -> O:
        """Attach ``value`` to ``owner`` and return ``owner``; call it once, when ``owner`` is created."""
        self._links[owner] = weakref.ref(value) if self._weak else value
        return owner

    def of(self, owner: object) -> T | None:
        """Return the value attached to ``owner``, or ``None`` if it has none (or cannot have one)."""
        try:
            value = self._links.get(owner)
        except TypeError:
            return None
        if self._weak and isinstance(value, weakref.ref):
            return typing.cast("T | None", value())
        return typing.cast("T | None", value)
