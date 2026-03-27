"""
Class-based API example for the Restate Python SDK.

This example demonstrates the same services as the decorator-based examples,
but using the class-based API with @handler, @shared, and @main decorators.
"""

import restate
from restate.cls import Service, VirtualObject, Workflow, handler, shared, main, Context


class Greeter(Service):
    """A simple stateless greeting service."""

    @handler
    async def greet(self, name: str) -> str:
        return f"Hello {name}!"


class Counter(VirtualObject):
    """A stateful counter backed by durable state."""

    @handler
    async def increment(self, value: int) -> int:
        n: int = await Context.get("counter", type_hint=int) or 0
        n += value
        Context.set("counter", n)
        return n

    @shared
    async def count(self) -> int:
        return await Context.get("counter", type_hint=int) or 0


class PaymentWorkflow(Workflow):
    """A durable payment workflow with external verification."""

    @main
    async def pay(self, amount: int) -> str:
        Context.set("status", "processing")

        async def charge():
            return f"charged ${amount}"

        receipt = await Context.run_typed("charge", charge)
        Context.set("status", "completed")
        return receipt

    @handler
    async def status(self) -> str:
        return await Context.get("status", type_hint=str) or "unknown"


app = restate.app([Greeter, Counter, PaymentWorkflow])
