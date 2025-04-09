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
# pylint: disable=C0301

import typing

from pydantic import BaseModel
from restate import wait_completed, Service, Context

# models
class GreetingRequest(BaseModel):
    name: str

class Greeting(BaseModel):
    messages: typing.List[str]

class Message(BaseModel):
    role: str
    content: str

concurrent_greeter = Service("concurrent_greeter")

@concurrent_greeter.handler()
async def greet(ctx: Context, req: GreetingRequest) -> Greeting:
    claude = ctx.service_call(claude_sonnet, arg=Message(role="user", content=f"please greet {req.name}"))
    openai = ctx.service_call(open_ai, arg=Message(role="user", content=f"please greet {req.name}"))

    pending, done = await wait_completed(claude, openai)

    # collect the completed greetings
    greetings = [await f for f in done]

    # cancel the pending calls
    for f in pending:
        await f.cancel_invocation() # type: ignore

    return Greeting(messages=greetings)


# not really interesting, just for this demo:

@concurrent_greeter.handler()
async def claude_sonnet(ctx: Context, req: Message) -> str:
    return f"Bonjour {req.content[13:]}!"

@concurrent_greeter.handler()
async def open_ai(ctx: Context, req: Message) -> str:
    return f"Hello {req.content[13:]}!"
