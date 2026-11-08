"""
A support agent whose tools are Restate itself: it looks orders up with a
plain service, waits for a manager's approval (a signal), and refunds with a
workflow. Every step is durable: a crash, or a manager who takes a day,
changes nothing::

    OPENAI_API_KEY=... uv run --extra openai --extra serve examples-agents/intermediate/support.py
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080
    curl localhost:8080/support/alice/chat -d 'Order 103 arrived broken, can I get my $30 back?'

The agent waits for approval. In another terminal, see what it waits for (the
refund, with the wait's id), and approve it::

    curl localhost:8080/support/alice/pending
    curl localhost:8080/support/alice/signal --json '{"id": "<the id>", "value": "approved"}'

or approve the oldest wait for an approval: ``{"name": "approval", "value": "approved"}``.
"""

import asyncio
from datetime import timedelta

from pydantic import BaseModel

import restate
from restate.agents import call, openai, wait_for_signal, workflow


class Order(BaseModel):
    order: str


class Refund(BaseModel):
    order: str
    amount: float


# A plain Restate service.
orders = restate.Service("orders")


@orders.handler()
async def status(_ctx: restate.Context, request: Order) -> str:
    return f"Order {request.order} was delivered yesterday."


# A workflow: a refund runs to the end, once, however long it takes.
refund = restate.Workflow("refund")


@refund.main()
async def run(ctx: restate.WorkflowContext, request: Refund) -> str:
    await ctx.sleep(timedelta(seconds=5))  # a stand-in for the payment provider
    return f"Refunded ${request.amount:g} for order {request.order}."


support = openai.agent(
    "support",
    system="You are a support agent for an online shop. Refund only what a manager approved.",
    tools=[
        call(status, description="The status of an order."),
        wait_for_signal(
            "approval",
            description="Wait for a manager to decide on a refund. Answers with the manager's decision.",
            input=Refund,
        ),
        workflow(run, description="Refund an order. Only once a manager approved it."),
    ],
)


if __name__ == "__main__":
    asyncio.run(restate.serve(restate.app([orders, refund, support])))
