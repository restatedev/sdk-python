"""
Class-based API example for the Restate Python SDK.

This example demonstrates the same services as the decorator-based examples,
but using the class-based API with @handler, @shared, and @main decorators.
"""

from datetime import timedelta

from pydantic import BaseModel

import restate
from restate.cls import Service, VirtualObject, Workflow, handler, shared, main, Restate


# ── Pydantic models ──


class GreetingRequest(BaseModel):
    name: str
    language: str = "en"


class GreetingResponse(BaseModel):
    message: str
    language: str


class Greeter(Service):
    """A simple stateless greeting service."""

    @handler
    async def greet(self, name: str) -> str:
        return f"Hello {name}!"


class Counter(VirtualObject):
    """A stateful counter backed by durable state."""

    @handler
    async def increment(self, value: int) -> int:
        n: int = await Restate.get("counter", type_hint=int) or 0
        n += value
        Restate.set("counter", n)
        return n

    @shared
    async def count(self) -> int:
        return await Restate.get("counter", type_hint=int) or 0


class PaymentWorkflow(Workflow):
    """A durable payment workflow with external verification."""

    @main
    async def pay(self, amount: int) -> str:
        Restate.set("status", "processing")

        async def charge():
            return f"charged ${amount}"

        receipt = await Restate.run("charge", charge)
        Restate.set("status", "completed")
        return receipt

    @handler
    async def status(self) -> str:
        return await Restate.get("status", type_hint=str) or "unknown"


class OrderProcessor(Service):
    """Demonstrates type-safe RPC between services using fluent proxies."""

    @handler
    async def process(self, customer: str) -> str:
        # Call a service handler — IDE knows .greet() takes str, returns str
        greeting = await Greeter.call().greet(customer)

        # Call a virtual object — IDE knows .increment() takes int, returns int
        count = await Counter.call(customer).increment(1)

        # Fire-and-forget send (returns SendHandle, not a coroutine)
        Counter.send(customer).increment(1)

        # Send with delay
        Counter.send(customer, delay=timedelta(seconds=30)).increment(1)

        # Call a workflow
        receipt = await PaymentWorkflow.call(f"order-{count}").pay(100)

        return f"{greeting} (visit #{count}, {receipt})"


class PydanticGreeter(Service):
    """Demonstrates Pydantic model serde with the class-based API."""

    def __init__(self, name):
        self.name = name

    @handler
    async def greet(self, req: GreetingRequest) -> GreetingResponse:
        greetings = {"en": "Hello", "es": "Hola", "de": "Hallo"}
        greeting = greetings.get(req.language, "Hello")

        async def translate() -> GreetingResponse:
            return GreetingResponse(message=f"{greeting} {req.name} from {self.name}", language=req.language)

        return await Restate.run("translate", translate)


# ── Service contract without implementation ──
#
# Define the shape of a service (handlers + types) without providing an
# implementation.  The proxy only needs class-level metadata created by
# __init_subclass__, so you can call a service that lives in another
# process — or is written in another language — just from its contract.


class ExternalInventory(VirtualObject, name="Inventory"):
    """Contract for an Inventory service whose implementation lives elsewhere."""

    @handler
    async def reserve(self, item_id: str) -> bool: ...  # type: ignore[empty-body]

    @handler
    async def current_stock(self) -> int: ...  # type: ignore[empty-body]


class Shop(Service):
    """Demonstrates calling a service defined only by its contract."""

    @handler
    async def buy(self, item_id: str) -> str:
        # Full IDE autocomplete — reserve(str) -> bool, current_stock() -> int
        ok = await ExternalInventory.call(item_id).reserve(item_id)
        if not ok:
            return "out of stock"
        stock = await ExternalInventory.call(item_id).current_stock()
        return f"reserved (remaining: {stock})"


app = restate.app([Greeter, Counter, PaymentWorkflow, OrderProcessor, PydanticGreeter("Restate"), Shop])
