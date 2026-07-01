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
"""list_object.py — class-based"""
# pylint: disable=C0116
# pylint: disable=W0613

from restate.cls import VirtualObject, handler, Restate


class ListObject(VirtualObject, name="ListObject"):

    @handler
    async def append(self, value: str):
        lst = await Restate.get("list") or []
        Restate.set("list", lst + [value])

    @handler
    async def get(self) -> list[str]:
        return await Restate.get("list") or []

    @handler
    async def clear(self) -> list[str]:
        result = await Restate.get("list") or []
        Restate.clear("list")
        return result
