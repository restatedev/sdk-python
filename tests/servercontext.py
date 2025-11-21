#
#  Copyright (c) 2023-2025 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#

import restate
from restate import (
    Context,
    RunOptions,
    Service,
    TerminalError,
)
import pytest

# ----- Asyncio fixtures


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [
    pytest.mark.anyio,
]


def ohoh():
    raise TerminalError("Simulated terminal error")


async def test_sanity():
    greeter = Service("greeter")

    @greeter.handler()
    async def greet(ctx: Context, name: str) -> str:
        await ctx.run_typed("foo", ohoh, RunOptions(max_attempts=3))
        return "hi"

    async with restate.create_test_harness(
        restate.app([greeter]), restate_image="ghcr.io/restatedev/restate:latest"
    ) as restate_test_harness:
        with pytest.raises(Exception):
            await restate_test_harness.client.service_call(greet, arg="bob")


async def test_wrapped_terminal_exception():
    greeter = Service("greeter")

    @greeter.handler()
    async def greet(ctx: Context, name: str) -> str:
        try:
            await ctx.run_typed("foo", ohoh)
            return "hi"
        except TerminalError as te:
            raise ValueError("Wrapped terminal error") from te

    async with restate.create_test_harness(
        restate.app([greeter]), restate_image="ghcr.io/restatedev/restate:latest"
    ) as restate_test_harness:
        with pytest.raises(Exception):
            await restate_test_harness.client.service_call(greet, arg="bob")
