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

from restate import VirtualObject, ObjectContext

list_object = VirtualObject("ListObject")

@list_object.handler()
async def append(ctx: ObjectContext, value: str):
    list = await ctx.get("list") or []
    ctx.set("list", list + [value])

@list_object.handler()
async def get(ctx: ObjectContext) -> list[str]:
    return await ctx.get("list") or []

@list_object.handler()
async def clear(ctx: ObjectContext) -> list[str]:
    result = await ctx.get("list") or []
    ctx.clear("list")
    return result
