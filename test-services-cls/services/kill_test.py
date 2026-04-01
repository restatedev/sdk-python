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

from restate.cls import VirtualObject, handler, Restate

from .awakeable_holder import AwakeableHolder


class KillTestRunner(VirtualObject, name="KillTestRunner"):

    @handler(name="startCallTree")
    async def start_call_tree(self):
        await KillTestSingleton.call(Restate.key()).recursive_call()


class KillTestSingleton(VirtualObject, name="KillTestSingleton"):

    @handler(name="recursiveCall")
    async def recursive_call(self):
        name, promise = Restate.awakeable()
        AwakeableHolder.send(Restate.key()).hold(name)  # type: ignore[unused-coroutine]
        await promise

        await KillTestSingleton.call(Restate.key()).recursive_call()

    @handler(name="isUnlocked")
    async def is_unlocked(self):
        return None
