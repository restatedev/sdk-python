#
#  Copyright (c) 2023-2024 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""example.py"""
# pylint: disable=C0116
# pylint: disable=W0613
# pylint: disable=W0622

from restate import Workflow, WorkflowContext, WorkflowSharedContext
from restate.exceptions import TerminalError

workflow = Workflow("BlockAndWaitWorkflow")

@workflow.main()
async def run(ctx: WorkflowContext, input: str):
    ctx.set("my-state", input)
    output = await ctx.promise("durable-promise").value()

    peek = await ctx.promise("durable-promise").peek()
    if peek is None:
        raise TerminalError(message="Durable promise should be completed")

    return output


@workflow.handler()
async def unblock(ctx: WorkflowSharedContext, output: str):
    await ctx.promise("durable-promise").resolve(output)

@workflow.handler(name="getState")
async def get_state(ctx: WorkflowSharedContext, output: str) -> str | None:
    return await ctx.get("my-state")
