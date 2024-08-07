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

failing = VirtualObject("Failing")

@failing.handler(name="terminallyFailingCall")
async def terminally_failing_call(ctx: ObjectContext, msg: str):
    raise TerminalError(message=msg)

@failing.handler(name="callTerminallyFailingCall")
async def call_terminally_failing_call(ctx: ObjectContext, msg: str) -> str:
    await ctx.object_call(terminally_failing_call,  key="random-583e1bf2", arg=msg)

    raise Exception("Should not reach here")

failures = 0

@failing.handler(name="failingCallWithEventualSuccess")
async def failing_call_with_eventual_success(ctx: ObjectContext) -> int:
    global failures
    failures += 1
    if failures >= 4:
        failures = 0
        return 4
    raise ValueError(f"Failed at attempt: {failures}")


side_effect_failures = 0

@failing.handler(name="failingSideEffectWithEventualSuccess")
async def failing_side_effect_with_eventual_success(ctx: ObjectContext) -> int:

    def side_effect():
        global side_effect_failures
        side_effect_failures += 1
        if side_effect_failures >= 4:
            side_effect_failures = 0
            return 4
        raise ValueError(f"Failed at attempt: {side_effect_failures}")

    return await ctx.run("sideEffect", side_effect) # type: ignore


@failing.handler(name="terminallyFailingSideEffect")
async def terminally_failing_side_effect(ctx: ObjectContext):

    def side_effect():
        raise TerminalError(message="failed side effect")

    await ctx.run("sideEffect", side_effect)
    raise ValueError("Should not reach here")
