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
    Service,
    HarnessEnvironment,
)
import pytest

# ----- Asyncio fixtures


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [
    pytest.mark.anyio,
]

# -------- Restate services and restate fixture

greeter = Service("greeter")


def magic_function():
    from restate.extensions import current_context

    ctx = current_context()
    assert ctx is not None
    return ctx.request().id


@greeter.handler()
async def greet(ctx: Context, name: str) -> str:
    id = magic_function()
    return f"Hello {id}!"


@pytest.fixture(scope="session")
async def restate_test_harness():
    async with restate.create_test_harness(
        restate.app([greeter]), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


# ----- Tests


async def test_greeter(restate_test_harness: HarnessEnvironment):
    greeting = await restate_test_harness.client.service_call(greet, arg="bob")
    assert greeting.startswith("Hello ")
