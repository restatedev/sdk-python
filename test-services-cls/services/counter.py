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
"""counter.py — class-based"""
# pylint: disable=C0116
# pylint: disable=W0613

from typing import TypedDict
from restate.cls import VirtualObject, handler, Restate
from restate.exceptions import TerminalError

COUNTER_KEY = "counter"


class CounterUpdateResponse(TypedDict):
    oldValue: int
    newValue: int


class Counter(VirtualObject, name="Counter"):

    @handler
    async def reset(self):
        Restate.clear(COUNTER_KEY)

    @handler
    async def get(self) -> int:
        c: int | None = await Restate.get(COUNTER_KEY)
        if c is None:
            return 0
        return c

    @handler
    async def add(self, addend: int) -> CounterUpdateResponse:
        old_value: int | None = await Restate.get(COUNTER_KEY)
        if old_value is None:
            old_value = 0
        new_value = old_value + addend
        Restate.set(COUNTER_KEY, new_value)
        return CounterUpdateResponse(oldValue=old_value, newValue=new_value)

    @handler(name="addThenFail")
    async def add_then_fail(self, addend: int):
        old_value: int | None = await Restate.get(COUNTER_KEY)
        if old_value is None:
            old_value = 0
        new_value = old_value + addend
        Restate.set(COUNTER_KEY, new_value)
        raise TerminalError(message=Restate.key())
