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

from restate import VirtualObject, ObjectContext, ObjectSharedContext

counter = VirtualObject("counter")

@counter.handler()
async def increment(ctx: ObjectContext, value: int) -> int:
    n = await ctx.get("counter", type_hint=int) or 0
    n += value
    ctx.set("counter", n)
    return n

@counter.handler(kind="shared")
async def count(ctx: ObjectSharedContext) -> int:
    return await ctx.get("counter") or 0
