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
from typing import Dict
from restate import VirtualObject, ObjectContext

from . import counter

invoke_counts: Dict[str, int] = {}

def do_left_action(ctx: ObjectContext) -> bool:
    count_key = ctx.key()
    invoke_counts[count_key] = invoke_counts.get(count_key, 0) + 1
    return invoke_counts[count_key] % 2 == 1

def increment_counter(ctx: ObjectContext):
    ctx.object_send(counter.add, key=ctx.key(), arg=1)

non_deterministic = VirtualObject("NonDeterministic")

@non_deterministic.handler(name="setDifferentKey")
async def set_different_key(ctx: ObjectContext):
    if do_left_action(ctx):
        ctx.set("a", "my-state")
    else:
        ctx.set("b", "my-state")
    await ctx.sleep(timedelta(milliseconds=100))
    increment_counter(ctx)

@non_deterministic.handler(name="backgroundInvokeWithDifferentTargets")
async def background_invoke_with_different_targets(ctx: ObjectContext):
    if do_left_action(ctx):
        ctx.object_send(counter.get, key="abc", arg=None)
    else:
        ctx.object_send(counter.reset, key="abc", arg=None)
    await ctx.sleep(timedelta(milliseconds=100))
    increment_counter(ctx)

@non_deterministic.handler(name="callDifferentMethod")
async def call_different_method(ctx: ObjectContext):
    if do_left_action(ctx):
        await ctx.object_call(counter.get, key="abc", arg=None)
    else:
        await ctx.object_call(counter.reset, key="abc", arg=None)
    await ctx.sleep(timedelta(milliseconds=100))
    increment_counter(ctx)

@non_deterministic.handler(name="eitherSleepOrCall")
async def either_sleep_or_call(ctx: ObjectContext):
    if do_left_action(ctx):
        await ctx.sleep(timedelta(milliseconds=100))
    else:
        await ctx.object_call(counter.get, key="abc", arg=None)
    await ctx.sleep(timedelta(milliseconds=100))
    increment_counter(ctx)
