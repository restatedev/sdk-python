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

from contextlib import asynccontextmanager
from restate.extensions import contextvar

import restate

from restate import Context, Service, HarnessEnvironment, extensions
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
    ctx = extensions.current_context()
    assert ctx is not None
    return ctx.request().id


@greeter.handler()
async def greet(ctx: Context, name: str) -> str:
    id = magic_function()
    return f"Hello {id}!"


# -- context manager


@contextvar
@asynccontextmanager
async def my_resource_manager():
    yield "hello"


@greeter.handler(invocation_context_managers=[my_resource_manager])
async def greet_with_cm(ctx: Context, name: str) -> str:
    return my_resource_manager.value


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


async def test_greeter_with_cm(restate_test_harness: HarnessEnvironment):
    greeting = await restate_test_harness.client.service_call(greet_with_cm, arg="bob")
    assert greeting == "hello"
