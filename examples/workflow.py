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
# pylint: disable=C0301

from datetime import timedelta

from restate import Workflow, WorkflowContext, WorkflowSharedContext
from restate import select
from restate import TerminalError

TIMEOUT = timedelta(seconds=10)

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

    await ctx.run_typed("payment", payment_gateway)

    ctx.set("status", "waiting for the payment provider to approve")

    # Wait for the payment to be verified

    match await select(result=ctx.promise("verify.payment").value(), timeout=ctx.sleep(TIMEOUT)):
        case ['result', "approved"]:
            ctx.set("status", "payment approved")
            return { "success" : True }
        case ['result', "declined"]:
            ctx.set("status", "payment declined")
            raise TerminalError(message="Payment declined", status_code=401)
        case ['timeout', _]:
            ctx.set("status", "payment verification timed out")
            raise TerminalError(message="Payment verification timed out", status_code=410)

@payment.handler()
async def payment_verified(ctx: WorkflowSharedContext, result: str):
    promise = ctx.promise("verify.payment", type_hint=str)
    await promise.resolve(result)
