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
from typing import Dict, List
from restate import Service, Context
from restate.serde import BytesSerde

test_utils = Service("TestUtilsService")

@test_utils.handler()
async def echo(context: Context, input: str) -> str:
    return input

@test_utils.handler(name="uppercaseEcho")
async def uppercase_echo(context: Context, input: str) -> str:
    return input.upper()

@test_utils.handler(name="echoHeaders")
async def echo_headers(context: Context) -> Dict[str, str]:
    return context.request().headers

@test_utils.handler(name="rawEcho", accept="*/*", content_type="application/octet-stream", input_serde=BytesSerde(), output_serde=BytesSerde())
async def raw_echo(context: Context, input: bytes) -> bytes:
    return input

@test_utils.handler(name="sleepConcurrently")
async def sleep_concurrently(context: Context, millis_duration: List[int]) -> None:
    timers = [context.sleep(timedelta(milliseconds=duration)) for duration in millis_duration]

    for timer in timers:
        await timer


@test_utils.handler(name="countExecutedSideEffects")
async def count_executed_side_effects(context: Context, increments: int) -> int:
    invoked_side_effects = 0

    def effect():
        nonlocal invoked_side_effects
        invoked_side_effects += 1

    for _ in range(increments):
        await context.run("count", effect)

    return invoked_side_effects

@test_utils.handler(name="cancelInvocation")
async def cancel_invocation(context: Context, invocation_id: str) -> None:
    context.cancel_invocation(invocation_id)
