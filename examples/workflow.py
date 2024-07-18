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
# pylint: disable=C0301


from restate import Workflow, WorkflowContext, WorkflowSharedContext
from restate.exceptions import TerminalError

payment = Workflow("payment")

@payment.main()
async def pay(ctx: WorkflowContext, amount: int):
    workflow_key = ctx.key()
    ctx.set("status", "verifying payment")

    # Call the payment service
    def payment_gateway():
        print("Please approve this payment: ")
        print("To approve use:")
        print(f"""curl http://localhost:8080/payment/{workflow_key}/payment_verified --json '"approved"' """)
        print("")
        print("To decline use:")
        print(f"""curl http://localhost:8080/payment/{workflow_key}/payment_verified --json '"declined"' """)

    await ctx.run("payment", payment_gateway)

    ctx.set("status", "waiting for the payment provider to approve")

    # Wait for the payment to be verified
    result = await ctx.promise("verify.payment").value()
    if result == "approved":
        ctx.set("status", "payment approved")
        return { "success" : True }

    ctx.set("status", "payment declined")
    raise TerminalError(message="Payment declined", status_code=401)

@payment.handler()
async def payment_verified(ctx: WorkflowSharedContext, result: str):
    promise = ctx.promise("verify.payment")
    await promise.resolve(result)
