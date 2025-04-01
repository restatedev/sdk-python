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

from datetime import timedelta

from pydantic import BaseModel
from restate import Service, Context
from restate import wait_completed, RestateDurableSleepFuture, RestateDurableCallFuture

from greeter import greet as g

# models
class GreetingRequest(BaseModel):
    name: str

class Greeting(BaseModel):
    message: str

# service

concurrent_greeter = Service("concurrent_greeter")


@concurrent_greeter.handler()
async def greet(ctx: Context, req: GreetingRequest) -> Greeting:
    g1 = ctx.service_call(g, arg="1")
    g2 = ctx.service_call(g, arg="2")
    g3 = ctx.sleep(timedelta(milliseconds=100))

    done, pending = await wait_completed(g1, g2, g3)

    for f in done:
        if isinstance(f, RestateDurableSleepFuture):
            print("Timeout :(x", flush=True)
        elif isinstance(f, RestateDurableCallFuture):
            # the result should be ready.
            print(await f)
    #
    # let's cancel the pending calls then
    #
    for f in pending:
        if isinstance(f, RestateDurableCallFuture):
            inv = await f.invocation_id()
            print(f"Canceling {inv}", flush=True)
            ctx.cancel_invocation(inv)

    return Greeting(message=f"Hello {req.name}!")
