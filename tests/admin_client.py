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
    VirtualObject,
    ObjectContext,
    ObjectSharedContext,
    HarnessEnvironment,
    AdminClient,
)
import pytest

# ----- Asyncio fixtures


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [
    pytest.mark.anyio,
]

# -------- Services with metadata and descriptions

greeter = Service(
    "greeter",
    description="A simple greeting service",
    metadata={"team": "platform", "version": "1.0"},
)


@greeter.handler(
    metadata={"a2a.restate.dev/handler": "true", "a2a.restate.dev/skill": "greeting"},
)
async def greet(ctx: Context, name: str) -> str:
    """Greets a person by name."""
    return f"Hello {name}!"


counter = VirtualObject(
    "counter",
    description="A durable counter",
)


@counter.handler()
async def increment(ctx: ObjectContext, value: int) -> int:
    n = await ctx.get("counter", type_hint=int) or 0
    n += value
    ctx.set("counter", n)
    return n


@counter.handler(kind="shared")
async def count(ctx: ObjectSharedContext) -> int:
    """Returns the current count."""
    return await ctx.get("counter") or 0


bare = Service("bare")


@bare.handler()
async def ping(ctx: Context) -> str:
    return "pong"


# -------- Harness fixture


@pytest.fixture(scope="session")
async def harness():
    async with restate.create_test_harness(
        restate.app([greeter, counter, bare]),
        restate_image="ghcr.io/restatedev/restate:latest",
    ) as env:
        yield env


# -------- Tests


async def test_list_services(harness: HarnessEnvironment):
    async with AdminClient(harness.admin_api_url) as admin:
        services = await admin.list_services()

    names = {s.name for s in services}
    assert "greeter" in names
    assert "counter" in names
    assert "bare" in names


async def test_get_service(harness: HarnessEnvironment):
    async with AdminClient(harness.admin_api_url) as admin:
        svc = await admin.get_service("greeter")

    assert svc.name == "greeter"
    assert svc.ty == "Service"
    assert svc.documentation == "A simple greeting service"
    assert svc.metadata == {"team": "platform", "version": "1.0"}


async def test_service_handlers(harness: HarnessEnvironment):
    async with AdminClient(harness.admin_api_url) as admin:
        svc = await admin.get_service("greeter")

    handler = svc.get_handler("greet")
    assert handler is not None
    assert handler.name == "greet"
    assert handler.documentation == "Greets a person by name."
    assert handler.metadata is not None
    assert handler.metadata["a2a.restate.dev/handler"] == "true"
    assert handler.metadata["a2a.restate.dev/skill"] == "greeting"


async def test_virtual_object_type(harness: HarnessEnvironment):
    async with AdminClient(harness.admin_api_url) as admin:
        svc = await admin.get_service("counter")

    assert svc.ty == "VirtualObject"
    assert svc.documentation == "A durable counter"

    inc = svc.get_handler("increment")
    assert inc is not None
    assert inc.ty == "Exclusive"

    cnt = svc.get_handler("count")
    assert cnt is not None
    assert cnt.ty == "Shared"
    assert cnt.documentation == "Returns the current count."


async def test_handler_not_found(harness: HarnessEnvironment):
    async with AdminClient(harness.admin_api_url) as admin:
        svc = await admin.get_service("bare")

    assert svc.get_handler("nonexistent") is None


async def test_service_not_found(harness: HarnessEnvironment):
    async with AdminClient(harness.admin_api_url) as admin:
        with pytest.raises(Exception):
            await admin.get_service("does_not_exist")


async def test_service_without_metadata(harness: HarnessEnvironment):
    async with AdminClient(harness.admin_api_url) as admin:
        svc = await admin.get_service("bare")

    assert svc.name == "bare"
    # Services without explicit metadata may have None or empty dict
    assert svc.metadata is None or svc.metadata == {}
    assert svc.documentation is None
