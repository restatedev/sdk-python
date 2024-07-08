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


from restate.service import Service
from restate.context import Context, ObjectContext
from restate.endpoint import endpoint
from restate.object import VirtualObject

greeter = Service("greeter")

@greeter.handler()
async def greet(ctx: Context, name: str) -> str:
    return f"Greet {name}"


@greeter.handler()
async def greet_different(ctx: Context, name: str) -> str:
    return "Just greet it f{name}"

counter = VirtualObject("counter")

@counter.handler()
async def increment(ctx: ObjectContext, value: int) -> int:
    return value + 1

@counter.handler(kind="shared")
async def count(ctx: ObjectContext, value: int) -> int:
    return value

app = endpoint().bind(greeter, counter).asgi_app()
