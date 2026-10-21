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

import httpx
import pytest

import restate
from restate import Context, ObjectContext, Service, VirtualObject, Workflow, WorkflowContext
from restate._preview import explicit_recovery
from restate.vm import VMWrapper


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


def test_is_off_by_default():
    greeter = Service("greeter")

    @greeter.handler()
    async def greet(ctx: Context, name: str) -> str:
        return name

    assert not greeter.explicit_recovery
    assert not greeter.handlers["greet"].explicit_recovery


def test_applies_to_every_handler_from_the_service():
    counter = explicit_recovery(VirtualObject("counter"))

    @counter.handler()
    async def add(ctx: ObjectContext, n: int) -> None:
        pass

    assert counter.explicit_recovery


def test_applies_to_a_single_handler():
    flow = Workflow("flow")

    @explicit_recovery
    @flow.main()
    async def run(ctx: WorkflowContext) -> None:
        pass

    @flow.handler()
    async def query(ctx: WorkflowContext) -> None:
        pass

    assert not flow.explicit_recovery
    assert flow.handlers["run"].explicit_recovery
    assert not flow.handlers["query"].explicit_recovery


def test_must_be_applied_on_top_of_the_handler_decorator():
    async def greet(ctx: Context, name: str) -> str:
        return name

    with pytest.raises(ValueError):
        explicit_recovery(greet)


def test_the_vm_takes_it():
    headers = [("content-type", "application/vnd.restate.invocation.v6")]
    VMWrapper(headers, explicit_recovery=True)
    VMWrapper(headers)


@pytest.mark.anyio
async def test_a_retry_runs_the_handler_again_and_entries_are_named():
    calls = 0
    counter = explicit_recovery(VirtualObject("counter"))

    def step() -> int:
        nonlocal calls
        calls += 1
        return calls

    @counter.handler(journal_retention=timedelta(hours=1))
    async def add(ctx: ObjectContext) -> str:
        attempt = await ctx.run_typed("step", step)  # replayed without explicit recovery
        ctx.set("attempt", attempt, entry_name="store attempt")
        if attempt == 1:
            raise ValueError("retry me")
        return ctx.request().id

    async with restate.create_test_harness(
        restate.app([counter]), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        invocation_id = await harness.client.object_call(add, key="a", arg=None)
        assert calls == 2

        async with httpx.AsyncClient() as http:
            response = await http.post(
                f"{harness.admin_api_url}/query",
                json={"query": f"SELECT entry_type, name FROM sys_journal WHERE id = '{invocation_id}'"},
                headers={"accept": "application/json"},
            )
        response.raise_for_status()
        names = [row["name"] for row in response.json()["rows"] if row["entry_type"] == "Command: SetState"]
        assert names == ["store attempt", "store attempt"]
