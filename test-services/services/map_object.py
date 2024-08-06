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
"""example.py"""
# pylint: disable=C0116
# pylint: disable=W0613

from typing import TypedDict
from restate import VirtualObject, ObjectContext

map_object = VirtualObject("MapObject")


class Entry(TypedDict):
    key: str
    value: str

@map_object.handler(name="set")
async def map_set(ctx: ObjectContext, entry: Entry):
    ctx.set(entry["key"], entry["value"])

@map_object.handler(name="get")
async def map_get(ctx: ObjectContext, key: str) -> str:
    return await ctx.get(key) or ""

@map_object.handler(name="clearAll")
async def map_clear_all(ctx: ObjectContext) -> list[Entry]:
    entries = []
    for key in await ctx.state_keys():
        value: str = await ctx.get(key) # type: ignore 
        entry = Entry(key=key, value=value)
        entries.append(entry)
        ctx.clear(key)
    return entries