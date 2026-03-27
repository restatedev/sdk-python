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
"""kill_test.py — class-based"""
# pylint: disable=C0116
# pylint: disable=W0613

from restate.cls import VirtualObject, handler, Context

from . import awakeable_holder


class KillTestRunner(VirtualObject, name="KillTestRunner"):

    @handler(name="startCallTree")
    async def start_call_tree(self):
        fn = KillTestSingleton._restate_handlers["recursiveCall"].fn
        await Context.object_call(fn, key=Context.key(), arg=None)


class KillTestSingleton(VirtualObject, name="KillTestSingleton"):

    @handler(name="recursiveCall")
    async def recursive_call(self):
        hold_fn = awakeable_holder.AwakeableHolder._restate_handlers["hold"].fn
        name, promise = Context.awakeable()
        Context.object_send(hold_fn, key=Context.key(), arg=name)
        await promise

        fn = KillTestSingleton._restate_handlers["recursiveCall"].fn
        await Context.object_call(fn, key=Context.key(), arg=None)

    @handler(name="isUnlocked")
    async def is_unlocked(self):
        return None
