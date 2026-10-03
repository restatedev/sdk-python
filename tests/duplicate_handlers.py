#
#  Copyright (c) 2023-2026 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
import pytest
import restate


def test_service_rejects_duplicate_handler_name():
    service = restate.Service("Greeter")

    @service.handler("greet")
    async def first(ctx: restate.Context) -> str:
        return "first"

    with pytest.raises(ValueError, match="Handler greet already exists in Greeter"):

        @service.handler("greet")
        async def second(ctx: restate.Context) -> str:
            return "second"


def test_virtual_object_rejects_duplicate_handler_name():
    obj = restate.VirtualObject("Counter")

    @obj.handler("add")
    async def first(ctx: restate.ObjectContext) -> int:
        return 1

    with pytest.raises(ValueError, match="Handler add already exists in Counter"):

        @obj.handler("add")
        async def second(ctx: restate.ObjectContext) -> int:
            return 2


def test_workflow_rejects_duplicate_handler_name():
    workflow = restate.Workflow("Signup")

    @workflow.main("run")
    async def run(ctx: restate.WorkflowContext) -> str:
        return "run"

    with pytest.raises(ValueError, match="Handler run already exists in Signup"):

        @workflow.handler("run")
        async def other(ctx: restate.WorkflowSharedContext) -> str:
            return "other"
