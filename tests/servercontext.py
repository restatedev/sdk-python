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
from datetime import timedelta
from restate.exceptions import RetryableError

from contextlib import asynccontextmanager
import restate
from support import RESTATE_IMAGE  # pylint: disable=import-error
from restate import (
    Context,
    HttpError,
    InvocationRetryPolicy,
    RunOptions,
    Service,
    TerminalError,
    VirtualObject,
    Workflow,
    WorkflowContext,
)
from restate.serde import DefaultSerde
from restate.server_context import ServerInvocationContext
import json
import pytest
import typing

# ----- Asyncio fixtures


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [
    pytest.mark.anyio,
    pytest.mark.server,
]


def ohoh():
    raise TerminalError("Simulated terminal error")


@asynccontextmanager
async def simple_harness(
    service: Service | VirtualObject | Workflow,
) -> typing.AsyncGenerator[restate.RestateClient, None]:
    async with restate.create_test_harness(restate.app([service]), restate_image=RESTATE_IMAGE) as restate_test_harness:
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


async def test_retryable_exception():
    greeter = Service("greeter")
    attempts = 0

    @greeter.handler(
        invocation_retry_policy=InvocationRetryPolicy(
            max_attempts=3,
            # Something really long to trigger a test timeout.
            # Default httpx2 client timeout is 5 seconds.
            initial_interval=timedelta(hours=1),
        ),
    )
    async def greet(ctx: Context, name: str) -> str:
        nonlocal attempts
        print(f"Attempt {attempts}")
        try:
            if attempts == 0:
                raise RetryableError("Simulated retryable error", retry_after=timedelta(milliseconds=100))
            else:
                raise TerminalError("Simulated terminal error")
        finally:
            attempts += 1

    async with simple_harness(greeter) as client:
        with pytest.raises(HttpError):  # Should be some sort of client error (not a timeout).
            await client.service_call(greet, arg="bob")

    assert attempts == 2


async def test_accidentally_wrapped_retryable_exception():
    greeter = Service("greeter")
    attempts = 0

    @greeter.handler(
        invocation_retry_policy=InvocationRetryPolicy(
            max_attempts=3,
            # Something really long to trigger a test timeout.
            # Default httpx2 client timeout is 5 seconds.
            initial_interval=timedelta(hours=1),
        ),
    )
    async def greet(ctx: Context, name: str) -> str:
        nonlocal attempts
        print(f"Attempt {attempts}")
        try:
            if attempts == 0:
                try:
                    raise RetryableError("Simulated retryable error", retry_after=timedelta(milliseconds=100))
                except RetryableError as re:
                    # Simulate a developer accidentally catching and wrapping a RetryableError, which should still
                    # be treated as retryable by the system.
                    raise ValueError("Wrapped retryable error") from re
            else:
                raise TerminalError("Simulated terminal error")
        finally:
            attempts += 1

    async with simple_harness(greeter) as client:
        with pytest.raises(HttpError):  # Should be some sort of client error (not a timeout).
            await client.service_call(greet, arg="bob")

    assert attempts == 2


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


async def test_attach_workflow_returns_the_result_of_its_run():
    greeter = Workflow("greeter_workflow")

    @greeter.main()
    async def hello(ctx: WorkflowContext) -> str:
        return "hi"

    caller = Service("caller")

    @caller.handler()
    async def send_and_attach(ctx: Context, workflow_id: str) -> str:
        ctx.workflow_send(hello, key=workflow_id, arg=None)  # starts the run, ignoring its result
        # attach_workflow is the server context's: the run's result, as JSON bytes.
        result = await typing.cast(ServerInvocationContext, ctx).attach_workflow("greeter_workflow", workflow_id)
        return json.loads(result)

    async with restate.create_test_harness(restate.app([greeter, caller]), restate_image=RESTATE_IMAGE) as harness:
        assert await harness.client.service_call(send_and_attach, arg="wf-1") == "hi"
