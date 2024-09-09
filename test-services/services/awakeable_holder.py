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
# pylint: disable=W0622

from restate import VirtualObject, ObjectContext
from restate.exceptions import TerminalError

awakeable_holder = VirtualObject("AwakeableHolder")

@awakeable_holder.handler()
async def hold(ctx: ObjectContext, id: str):
    ctx.set("id", id)

@awakeable_holder.handler(name="hasAwakeable")
async def has_awakeable(ctx: ObjectContext) -> bool:
    res = await ctx.get("id")
    return res is not None

@awakeable_holder.handler()
async def unlock(ctx: ObjectContext, payload: str):
    id = await ctx.get("id")
    if id is None:
        raise TerminalError(message="No awakeable is registered")
    ctx.resolve_awakeable(id, payload)
