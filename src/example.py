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


from datetime import timedelta
from restate.service import Service
from restate.context import Context, ObjectContext, ObjectSharedContext
from restate.context import WorkflowContext, WorkflowSharedContext
from restate.object import VirtualObject
from restate.workflow import Workflow
import restate

greeter = Service("greeter")

@greeter.handler()
async def greet(ctx: Context, name: str) -> str:
    ctx.service_send(greet_different, arg=name, send_delay=timedelta(seconds=3))
    await ctx.sleep(delta=timedelta(seconds=3))
    return f"Greet {name} here is a greeting just for you "


@greeter.handler()
async def greet_different(ctx: Context, name: str) -> str:
    ctx.object_send(increment, key=name, arg=1, send_delay=timedelta(seconds=3))
    await ctx.object_call(increment, key=name, arg=1)
    return f"Just greet it {name}"

counter = VirtualObject("counter")

@counter.handler()
async def increment(ctx: ObjectContext, value: int) -> int:
    n = await ctx.get("counter") or 0
    n += value
    ctx.set("counter", n)
    return n

@counter.handler(kind="shared")
async def count(ctx: ObjectSharedContext) -> int:
    return await ctx.get("counter") or 0


payment = Workflow("payment")

@payment.main()
async def pay(ctx: WorkflowContext, amount: int) -> int:
    promise = ctx.promise("email.clicked")
    print(await promise.value())
    return amount

@payment.handler()
async def email_clicked(ctx: WorkflowSharedContext, secret: str):
    promise = ctx.promise("email.clicked")
    await promise.resolve(secret)


app = restate.app(services=[greeter, counter, payment])
