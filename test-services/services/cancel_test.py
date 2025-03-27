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

from datetime import timedelta
from typing import Literal

from restate import VirtualObject, ObjectContext
from restate.exceptions import TerminalError

from . import awakeable_holder

BlockingOperation = Literal["CALL", "SLEEP", "AWAKEABLE"]

runner = VirtualObject("CancelTestRunner")

@runner.handler(name="startTest")
async def start_test(ctx: ObjectContext, op: BlockingOperation):
    try:
        await ctx.object_call(block, key=ctx.key(), arg=op)
    except TerminalError as t:
        if t.status_code == 409:
            ctx.set("state", True)
        else:
            raise t

@runner.handler(name="verifyTest")
async def verify_test(ctx: ObjectContext) -> bool:
    state = await ctx.get("state")
    if state is None:
        return False
    return state

                                  
blocking_service = VirtualObject("CancelTestBlockingService")

@blocking_service.handler()
async def block(ctx: ObjectContext, op: BlockingOperation):
    name, awakeable = ctx.awakeable()
    await ctx.object_call(awakeable_holder.hold, key=ctx.key(), arg=name)
    await awakeable

    if op == "CALL":
        await ctx.object_call(block, key=ctx.key(), arg=op)
    elif op == "SLEEP":
        await ctx.sleep(timedelta(days=1024))
    elif op == "AWAKEABLE":
        name, uncompleteable = ctx.awakeable()
        await uncompleteable

@blocking_service.handler(name="isUnlocked")
async def is_unlocked(ctx: ObjectContext):
    return None
