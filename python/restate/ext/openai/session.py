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
"""
This module contains the optional OpenAI integration for Restate.
"""

from agents.memory.session import SessionABC
from agents.items import TResponseInputItem
from typing import List, cast

from restate.extensions import current_context
from restate import ObjectContext


class RestateSession(SessionABC):
    """Restate session implementation following the Session protocol."""

    def __init__(self):
        self._items: List[TResponseInputItem] | None = None

    def _ctx(self) -> ObjectContext:
        return cast(ObjectContext, current_context())

    async def get_items(self, limit: int | None = None) -> List[TResponseInputItem]:
        """Retrieve conversation history for this session."""
        if self._items is None:
            self._items = await self._ctx().get("items") or []
        if limit is not None:
            return self._items[-limit:]
        return self._items.copy()

    async def add_items(self, items: List[TResponseInputItem]) -> None:
        """Store new items for this session."""
        if self._items is None:
            self._items = await self._ctx().get("items") or []
        self._items.extend(items)

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from this session."""
        if self._items is None:
            self._items = await self._ctx().get("items") or []
        if self._items:
            return self._items.pop()
        return None

    def flush(self) -> None:
        """Flush the session items to the context."""
        self._ctx().set("items", self._items)

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        self._items = []
        self._ctx().clear("items")
