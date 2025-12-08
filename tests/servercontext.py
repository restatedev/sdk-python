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
import restate
from restate import (
    Context,
    RunOptions,
    Service,
    TerminalError,
    VirtualObject,
    Workflow,
    WorkflowContext,
)
from restate.serde import DefaultSerde
import pytest
import typing

# ----- Asyncio fixtures


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [
    pytest.mark.anyio,
]


def ohoh():
    raise TerminalError("Simulated terminal error")


@asynccontextmanager
async def simple_harness(service: Service | VirtualObject | Workflow) -> typing.AsyncIterator[restate.RestateClient]:
    async with restate.create_test_harness(
        restate.app([service]), restate_image="ghcr.io/restatedev/restate:latest"
    ) as restate_test_harness:
        yield restate_test_harness.client


async def test_sanity():
    greeter = Service("greeter")

    @greeter.handler()
    async def greet(ctx: Context, name: str) -> str:
        await ctx.run_typed("foo", ohoh, RunOptions(max_attempts=3))
        return "hi"

    async with simple_harness(greeter) as client:
        with pytest.raises(Exception):
            await client.service_call(greet, arg="bob")


async def test_wrapped_terminal_exception():
    greeter = Service("greeter")

    @greeter.handler()
    async def greet(ctx: Context, name: str) -> str:
        try:
            await ctx.run_typed("foo", ohoh)
            return "hi"
        except TerminalError as te:
            raise ValueError("Wrapped terminal error") from te

    async with simple_harness(greeter) as client:
        with pytest.raises(Exception):
            await client.service_call(greet, arg="bob")


async def test_promise_default_serde():
    workflow = Workflow("test_workflow")

    @workflow.main()
    async def run(ctx: WorkflowContext) -> str:
        promise = ctx.promise("test.promise", type_hint=str)

        assert isinstance(promise.serde, DefaultSerde), f"Expected DefaultSerde but got {type(promise.serde).__name__}"

        await promise.resolve("success")
        return await promise.value()

    async with simple_harness(workflow) as client:
        result = await client.workflow_call(run, key="test-key", arg=None)
        assert result == "success"


async def test_handler_with_union_none():
    greeter = Service("greeter")

    @greeter.handler()
    async def greet(ctx: Context, name: str) -> str | None:
        return "hi"

    async with simple_harness(greeter) as client:
        res = await client.service_call(greet, arg="bob")
        assert res == "hi"


async def test_handler_with_ctx_none():
    greeter = Service("greeter")

    async def maybe_something() -> str | None:
        return "hi"

    @greeter.handler()
    async def greet(ctx: Context, name: str) -> str | None:
        return await ctx.run_typed("foo", maybe_something)

    async with simple_harness(greeter) as client:
        res = await client.service_call(greet, arg="bob")
        assert res == "hi"
