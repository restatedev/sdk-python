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
"""cancel_test.py — class-based"""
# pylint: disable=C0116
# pylint: disable=W0613

from datetime import timedelta
from typing import Literal
from restate.cls import VirtualObject, handler, Restate
from restate.exceptions import TerminalError

from . import awakeable_holder

BlockingOperation = Literal["CALL", "SLEEP", "AWAKEABLE"]


class CancelTestRunner(VirtualObject, name="CancelTestRunner"):

    @handler(name="startTest")
    async def start_test(self, op: BlockingOperation):
        try:
            await CancelTestBlockingService.call(Restate.key()).block(op)
        except TerminalError as t:
            if t.status_code == 409:
                Restate.set("state", True)
            else:
                raise t

    @handler(name="verifyTest")
    async def verify_test(self) -> bool:
        state = await Restate.get("state")
        if state is None:
            return False
        return state


class CancelTestBlockingService(VirtualObject, name="CancelTestBlockingService"):

    @handler
    async def block(self, op: BlockingOperation):
        name, awakeable = Restate.awakeable()
        awakeable_holder.AwakeableHolder.send(Restate.key()).hold(name)
        await awakeable

        if op == "CALL":
            await CancelTestBlockingService.call(Restate.key()).block(op)
        elif op == "SLEEP":
            await Restate.sleep(timedelta(days=1024))
        elif op == "AWAKEABLE":
            name, uncompleteable = Restate.awakeable()
            await uncompleteable

    @handler(name="isUnlocked")
    async def is_unlocked(self):
        return None
