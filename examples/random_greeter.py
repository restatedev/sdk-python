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

random_greeter = Service("random_greeter")

@random_greeter.handler()
async def greet(ctx: Context, name: str) -> str:

    random_number = ctx.random().randint(0, 100)
    random_uuid = ctx.uuid()

    return f"Hello {name} with random number {random_number} and uuid {random_uuid}!"
