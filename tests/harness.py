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

import uuid
import restate
from restate import (
    Context,
    Service,
    HarnessEnvironment,
    VirtualObject,
    ObjectContext,
    ObjectSharedContext,
    Workflow,
    WorkflowContext,
    getLogger,
    WorkflowSharedContext,
)
import pytest
import asyncio

# ----- Asyncio fixtures


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [
    pytest.mark.anyio,
]

# -------- Restate services and restate fixture

greeter = Service("greeter")


@greeter.handler()
async def greet(ctx: Context, name: str) -> str:
    return f"Hello {name}!"


counter = VirtualObject("counter")


@counter.handler()
async def increment(ctx: ObjectContext, value: int) -> int:
    n = await ctx.get("counter", type_hint=int) or 0
    n += value
    ctx.set("counter", n)
    return n


@counter.handler(kind="shared")
async def count(ctx: ObjectSharedContext) -> int:
    return await ctx.get("counter") or 0


payment = Workflow("payment")
payment_logger = getLogger("payment")


@payment.main()
async def pay(ctx: WorkflowContext):
    ctx.set("status", "verifying payment")

    def payment_gateway():
        payment_logger.info("Doing payment work")

    await ctx.run_typed("payment", payment_gateway)

    ctx.set("status", "waiting for the payment provider to approve")

    # Wait for the payment to be verified
    result = await ctx.promise("verify.payment", type_hint=str).value()
    return f"Verified {result}!"


@payment.handler()
async def payment_verified(ctx: WorkflowSharedContext, result: str):
    promise = ctx.promise("verify.payment", type_hint=str)
    await promise.resolve(result)


@pytest.fixture(scope="session")
async def restate_test_harness():
    async with restate.create_test_harness(
        restate.app([greeter, counter, payment]), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield harness


# ----- Tests


async def test_greeter(restate_test_harness: HarnessEnvironment):
    greeting = await restate_test_harness.client.service_call(greet, arg="Pippo")

    assert greeting == "Hello Pippo!"


async def test_counter(restate_test_harness: HarnessEnvironment):
    random_key = str(uuid.uuid4())
    initial_count = await restate_test_harness.client.object_call(count, key=random_key, arg=None)
    await restate_test_harness.client.object_call(increment, key=random_key, arg=1)
    new_count = await restate_test_harness.client.object_call(count, key=random_key, arg=None)

    assert new_count == initial_count + 1


async def test_idempotency_key(restate_test_harness: HarnessEnvironment):
    random_key = str(uuid.uuid4())
    initial_count = await restate_test_harness.client.object_call(count, key=random_key, arg=None)
    await restate_test_harness.client.object_call(increment, key=random_key, arg=1, idempotency_key=random_key)
    await restate_test_harness.client.object_call(increment, key=random_key, arg=1, idempotency_key=random_key)
    new_count = await restate_test_harness.client.object_call(count, key=random_key, arg=None)

    assert new_count == initial_count + 1


async def test_workflow(restate_test_harness: HarnessEnvironment):
    random_key = str(uuid.uuid4())
    call_task = asyncio.create_task(restate_test_harness.client.workflow_call(pay, key=random_key, arg=None))

    await restate_test_harness.client.workflow_call(payment_verified, key=random_key, arg="Done")

    assert await call_task == "Verified Done!"


async def test_send(restate_test_harness: HarnessEnvironment):
    invocation_handle = await restate_test_harness.client.service_send(greet, arg="Pippo")

    assert invocation_handle.status_code == 200
    assert len(invocation_handle.invocation_id) > 0
