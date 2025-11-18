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
from restate import Context, Service, HarnessEnvironment
import pytest


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [
    pytest.mark.anyio,
]

greeter = Service("greeter")


@greeter.handler()
async def greet(ctx: Context, name: str) -> str:
    return f"Hello {name}!"


@pytest.fixture(scope="session")
async def restate_test_harness():
    async with restate.create_test_harness(
        restate.app([greeter]), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


async def test_with_harness(restate_test_harness: HarnessEnvironment):
    greeting = await restate_test_harness.client.service_call(greet, arg="Pippo")

    assert greeting == "Hello Pippo!"
