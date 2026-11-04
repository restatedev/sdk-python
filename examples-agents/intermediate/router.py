"""
A front desk that routes each message to the team that handles it: a handler
of your own, where your code decides and the agents answer.

The desk asks a model which team it is (``pydantic_ai.prompt``, answered
once), then calls that team's agent like any Restate handler. Each team keeps
its own conversation with each user (``billing/alice``, ``tech/alice``)::

    OPENAI_API_KEY=... uv run --extra openai --extra langgraph --extra pydantic_ai --extra serve \\
        --with langchain-openai --with 'pydantic-ai-slim[openai]' examples-agents/intermediate/router.py
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080
    curl localhost:8080/desk/alice/route --json '"I was charged twice for order 103"'
    curl localhost:8080/desk/alice/route --json '"The app crashes when I open my orders"'
    curl localhost:8080/billing/alice/messages --json '{}'   # what billing was asked, and answered
"""

import asyncio

import restate
from restate import sessions
from restate.agents import langgraph, openai, pydantic_ai

billing = langgraph.agent(
    "billing",
    system="You handle billing: charges, refunds and invoices. Be brief and kind.",
)

tech = openai.agent(
    "tech",
    system="You handle technical problems with the app. Ask for what you need to know, one question at a time.",
)

desk = sessions.SessionObject("desk")


@desk.handler
async def route(ctx: sessions.SessionContext, message: str) -> str:
    # Asked once: a retried attempt gets the same answer.
    team = await pydantic_ai.prompt(
        ctx, f"Which team handles this message, billing or tech? Answer with the one word.\n\n{message}"
    )
    agent = billing if "billing" in team.lower() else tech
    # A handler is not replayed: the idempotency key makes a retried attempt
    # reach the call that the first one made, rather than send the message again.
    return await ctx.generic_call(
        agent.name, "chat", message, key=ctx.key, idempotency_key=f"{ctx.request.id}:{agent.name}"
    )


if __name__ == "__main__":
    asyncio.run(restate.serve(sessions.app(desk, billing, tech)))
