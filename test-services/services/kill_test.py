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

from . import awakeable_holder

kill_runner = VirtualObject("KillTestRunner")

@kill_runner.handler(name="startCallTree")
async def start_call_tree(ctx: ObjectContext):
    await ctx.object_call(recursive_call, key=ctx.key(), arg=None)

kill_singleton = VirtualObject("KillTestSingleton")

@kill_singleton.handler(name="recursiveCall")
async def recursive_call(ctx: ObjectContext):
    name, promise = ctx.awakeable()
    ctx.object_send(awakeable_holder.hold, key=ctx.key(), arg=name)
    await promise

    await ctx.object_call(recursive_call, key=ctx.key(), arg=None)

@kill_singleton.handler(name="isUnlocked")
async def is_unlocked(ctx: ObjectContext):
    return None
