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
"""test_utils.py — class-based"""
# pylint: disable=C0116
# pylint: disable=W0613

from datetime import timedelta
from typing import Dict, List
from restate.cls import Service, handler, Restate
from restate.serde import BytesSerde


class TestUtilsService(Service, name="TestUtilsService"):

    @handler
    async def echo(self, input: str) -> str:
        return input

    @handler(name="uppercaseEcho")
    async def uppercase_echo(self, input: str) -> str:
        return input.upper()

    @handler(name="echoHeaders")
    async def echo_headers(self) -> Dict[str, str]:
        return Restate.request().headers

    @handler(name="sleepConcurrently")
    async def sleep_concurrently(self, millis_duration: List[int]) -> None:
        timers = [Restate.sleep(timedelta(milliseconds=duration)) for duration in millis_duration]
        for timer in timers:
            await timer

    @handler(name="countExecutedSideEffects")
    async def count_executed_side_effects(self, increments: int) -> int:
        invoked_side_effects = 0

        def effect():
            nonlocal invoked_side_effects
            invoked_side_effects += 1

        for _ in range(increments):
            await Restate.run("count", effect)

        return invoked_side_effects

    @handler(name="cancelInvocation")
    async def cancel_invocation(self, invocation_id: str) -> None:
        Restate.cancel_invocation(invocation_id)

    @handler(
        name="rawEcho",
        accept="*/*",
        content_type="application/octet-stream",
        input_serde=BytesSerde(),
        output_serde=BytesSerde(),
    )
    async def raw_echo(self, input: bytes) -> bytes:
        return input
