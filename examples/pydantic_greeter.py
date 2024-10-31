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
"""greeter.py"""
# pylint: disable=C0116
# pylint: disable=W0613
# pylint: disable=C0115
# pylint: disable=R0903

from pydantic import BaseModel
from restate import Service, Context

# models
class GreetingRequest(BaseModel):
    name: str

class Greeting(BaseModel):
    message: str

# service

pydantic_greeter = Service("pydantic_greeter")

@pydantic_greeter.handler()
async def greet(ctx: Context, req: GreetingRequest) -> Greeting:
    return Greeting(message=f"Hello {req.name}!")
