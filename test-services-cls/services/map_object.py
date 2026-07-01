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
"""map_object.py — class-based"""
# pylint: disable=C0116
# pylint: disable=W0613

from typing import TypedDict
from restate.cls import VirtualObject, handler, Restate


class Entry(TypedDict):
    key: str
    value: str


class MapObject(VirtualObject, name="MapObject"):

    @handler(name="set")
    async def map_set(self, entry: Entry):
        Restate.set(entry["key"], entry["value"])

    @handler(name="get")
    async def map_get(self, key: str) -> str:
        return await Restate.get(key) or ""

    @handler(name="clearAll")
    async def map_clear_all(self) -> list[Entry]:
        entries = []
        for key in await Restate.state_keys():
            value: str = await Restate.get(key)  # type: ignore
            entry = Entry(key=key, value=value)
            entries.append(entry)
            Restate.clear(key)
        return entries
