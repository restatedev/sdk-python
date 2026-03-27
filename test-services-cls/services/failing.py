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
"""failing.py — class-based"""

from datetime import timedelta

# pylint: disable=C0116
# pylint: disable=W0613
# pylint: disable=W0622

from restate.cls import VirtualObject, handler, Context
from restate.exceptions import TerminalError
from restate import RunOptions

failures = 0
eventual_success_side_effects = 0
eventual_failure_side_effects = 0


class Failing(VirtualObject, name="Failing"):

    @handler(name="terminallyFailingCall")
    async def terminally_failing_call(self, msg: str):
        raise TerminalError(message=msg)

    @handler(name="callTerminallyFailingCall")
    async def call_terminally_failing_call(self, msg: str) -> str:
        fn = Failing._restate_handlers["terminallyFailingCall"].fn
        await Context.object_call(fn, key="random-583e1bf2", arg=msg)
        raise Exception("Should not reach here")

    @handler(name="failingCallWithEventualSuccess")
    async def failing_call_with_eventual_success(self) -> int:
        global failures
        failures += 1
        if failures >= 4:
            failures = 0
            return 4
        raise ValueError(f"Failed at attempt: {failures}")

    @handler(name="terminallyFailingSideEffect")
    async def terminally_failing_side_effect(self, error_message: str):
        def side_effect():
            raise TerminalError(message=error_message)

        await Context.run_typed("sideEffect", side_effect)
        raise ValueError("Should not reach here")

    @handler(name="sideEffectSucceedsAfterGivenAttempts")
    async def side_effect_succeeds_after_given_attempts(self, minimum_attempts: int) -> int:
        def side_effect():
            global eventual_success_side_effects
            eventual_success_side_effects += 1
            if eventual_success_side_effects >= minimum_attempts:
                return eventual_success_side_effects
            raise ValueError(f"Failed at attempt: {eventual_success_side_effects}")

        options: RunOptions[int] = RunOptions(
            max_attempts=minimum_attempts + 1, initial_retry_interval=timedelta(milliseconds=1), retry_interval_factor=1.0
        )
        return await Context.run_typed("sideEffect", side_effect, options)

    @handler(name="sideEffectFailsAfterGivenAttempts")
    async def side_effect_fails_after_given_attempts(self, retry_policy_max_retry_count: int) -> int:
        def side_effect():
            global eventual_failure_side_effects
            eventual_failure_side_effects += 1
            raise ValueError(f"Failed at attempt: {eventual_failure_side_effects}")

        try:
            options: RunOptions[int] = RunOptions(
                max_attempts=retry_policy_max_retry_count,
                initial_retry_interval=timedelta(milliseconds=1),
                retry_interval_factor=1.0,
            )
            await Context.run_typed("sideEffect", side_effect, options)
            raise ValueError("Side effect did not fail.")
        except TerminalError:
            global eventual_failure_side_effects
            return eventual_failure_side_effects
