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
import restate

# Use restate.getLogger to create a logger that hides logs on replay
# To configure logging, just use the usual std logging configuration (see example.py for an example)
logger = restate.getLogger()

greeter = Service("greeter")

@greeter.handler()
async def greet(ctx: Context, name: str) -> str:
    logger.info("Received greeting request: %s", name)
    return f"Hello {name}!"
