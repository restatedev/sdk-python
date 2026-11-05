"""
An agent that follows up later: it schedules a check-in with the customer a
day from now, a one-way call that Restate keeps until then. Nothing runs in
the meantime, and a restart changes nothing::

    OPENAI_API_KEY=... uv run --extra pydantic_ai --extra serve --with 'pydantic-ai-slim[openai]' examples-agents/intermediate/followup.py
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080
    curl localhost:8080/concierge/alice/chat -d 'My order 103 is late. Can you keep an eye on it?'

The check-in shows in Restate's UI (http://localhost:9070) as a scheduled
call of ``checkins/remind``. With FOLLOW_UP_SECONDS=10 it comes sooner.
"""

import asyncio
import os
from datetime import timedelta

from pydantic import BaseModel

import restate
from restate.agents import pydantic_ai, send


class Checkin(BaseModel):
    customer: str
    note: str


# A stand-in for an email or a message to the customer.
checkins = restate.Service("checkins")


@checkins.handler()
async def remind(_ctx: restate.Context, checkin: Checkin) -> None:
    print(f"Checking in with {checkin.customer}: {checkin.note}")


concierge = pydantic_ai.agent(
    "concierge",
    system="You are a concierge for an online shop. When a customer needs it, schedule a check-in.",
    tools=[
        send(
            remind,
            name="schedule_checkin",
            description="Check in with the customer a day from now, with a note of what to check.",
            delay=timedelta(seconds=int(os.environ.get("FOLLOW_UP_SECONDS", 24 * 60 * 60))),
        ),
    ],
)


if __name__ == "__main__":
    asyncio.run(restate.serve(restate.app([checkins, concierge])))
