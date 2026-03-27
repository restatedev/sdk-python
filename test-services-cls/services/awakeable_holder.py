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
"""awakeable_holder.py — class-based"""
# pylint: disable=C0116
# pylint: disable=W0613
# pylint: disable=W0622

from restate.cls import VirtualObject, handler, Context
from restate.exceptions import TerminalError


class AwakeableHolder(VirtualObject, name="AwakeableHolder"):

    @handler
    async def hold(self, id: str):
        Context.set("id", id)

    @handler(name="hasAwakeable")
    async def has_awakeable(self) -> bool:
        res = await Context.get("id")
        return res is not None

    @handler
    async def unlock(self, payload: str):
        id = await Context.get("id")
        if id is None:
            raise TerminalError(message="No awakeable is registered")
        Context.resolve_awakeable(id, payload)
