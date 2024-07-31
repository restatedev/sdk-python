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

from restate import Service, Context
from receiver import ping as receiver_ping
import uuid

coordinator = Service("Coordinator")

@coordinator.handler()
async def proxy(ctx: Context) -> str:
    key = await ctx.run("key", lambda: str(uuid.uuid4()))
    return await ctx.object_call(receiver_ping, key=key, arg=None)
