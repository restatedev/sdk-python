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
"""block_and_wait_workflow.py — class-based"""
# pylint: disable=C0116
# pylint: disable=W0613
# pylint: disable=W0622

from restate.cls import Workflow, handler, main, Context
from restate.exceptions import TerminalError


class BlockAndWaitWorkflow(Workflow, name="BlockAndWaitWorkflow"):

    @main
    async def run(self, input: str):
        Context.set("my-state", input)
        output = await Context.promise("durable-promise").value()

        peek = await Context.promise("durable-promise").peek()
        if peek is None:
            raise TerminalError(message="Durable promise should be completed")

        return output

    @handler
    async def unblock(self, output: str):
        await Context.promise("durable-promise").resolve(output)

    @handler(name="getState")
    async def get_state(self, output: str) -> str | None:
        return await Context.get("my-state")
