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
from restate.exceptions import TerminalError

counter = VirtualObject("Counter")

COUNTER_KEY = "counter"


@counter.handler()
async def reset(ctx: ObjectContext):
    ctx.clear(COUNTER_KEY)


@counter.handler()
async def get(ctx: ObjectContext) -> int:
    c: int | None = await ctx.get(COUNTER_KEY)
    if c is None:
        return 0
    return c


class CounterUpdateResponse(TypedDict):
    oldValue: int
    newValue: int


@counter.handler()
async def add(ctx: ObjectContext, addend: int) -> CounterUpdateResponse:
    old_value: int | None = await ctx.get(COUNTER_KEY)
    if old_value is None:
        old_value = 0
    new_value = old_value + addend
    ctx.set(COUNTER_KEY, new_value)
    return CounterUpdateResponse(oldValue=old_value, newValue=new_value)


@counter.handler()
async def addThenFail(ctx: ObjectContext, addend: int):
    old_value: int | None = await ctx.get(COUNTER_KEY)
    if old_value is None:
        old_value = 0
    new_value = old_value + addend
    ctx.set(COUNTER_KEY, new_value)

    raise TerminalError(message=ctx.key())