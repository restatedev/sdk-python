#
#  Copyright (c) 2023-2024 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Node.js/TypeScript,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""example.py"""
# pylint: disable=C0116
# pylint: disable=W0613

import asyncio

import uvicorn

from restate.service import Service
from restate.context import Context, ObjectContext
from restate.endpoint import Endpoint
from restate.object import VirtualObject


greeter = Service("greeter")

@greeter.handler()
async def greet(ctx: Context, name: str) -> str:
    #ctx.service_call(greet_different, "asdads")
    return f"Greet {name}"


@greeter.handler()
async def greet_different(ctx: Context, name: str) -> str:
    return "Just greet it f{name}"

counter = VirtualObject("counter")

@counter.handler()
async def increment(ctx: ObjectContext, value: int) -> int:
    return value + 1

@counter.handler(kind="shared")
def count(ctx: ObjectContext, value: int) -> int:
    return value


async def main():
    from restate.server import server
    app = server(Endpoint().bind(greeter).bind(counter))
    config = uvicorn.Config(app, port=9080, log_level="debug")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
        asyncio.run(main())
