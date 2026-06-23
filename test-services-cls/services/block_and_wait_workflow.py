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

from restate.cls import Workflow, handler, main, Restate
from restate.exceptions import TerminalError


class BlockAndWaitWorkflow(Workflow, name="BlockAndWaitWorkflow"):

    @main
    async def run(self, input: str):
        Restate.set("my-state", input)
        output = await Restate.promise("durable-promise").value()

        peek = await Restate.promise("durable-promise").peek()
        if peek is None:
            raise TerminalError(message="Durable promise should be completed")

        return output

    @handler
    async def unblock(self, output: str):
        await Restate.promise("durable-promise").resolve(output)

    @handler(name="getState")
    async def get_state(self, output: str) -> str | None:
        return await Restate.get("my-state")
