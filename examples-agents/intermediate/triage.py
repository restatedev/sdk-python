"""
Agents as steps of a workflow: one agent classifies a support ticket, another
drafts a reply, and a person reviews the draft before it goes out. The
workflow calls the agents like any Restate handler, and waits for the review
without holding a process::

    OPENAI_API_KEY=... uv run --extra langgraph --extra pydantic_ai --extra serve \\
        --with langchain-openai --with 'pydantic-ai-slim[openai]' examples-agents/intermediate/triage.py
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080
    curl localhost:8080/triage/t1/run/send --json '"I was charged twice for order 103."'
    curl localhost:8080/triage/t1/draft                                           # the drafted reply
    curl localhost:8080/triage/t1/review --json '"Sorry about that! We refunded the second charge."'
    curl localhost:8080/restate/workflow/triage/t1/output                         # what went out
"""

import asyncio
import json

import restate
from restate.agents import AgentObject, langgraph, pydantic_ai

classifier = langgraph.agent(
    "classifier",
    system="Classify a support ticket. Answer with one word: billing, bug, or other.",
)

drafter = pydantic_ai.agent(
    "drafter",
    system="Draft a short, kind reply to a support ticket.",
)

triage = restate.Workflow("triage")


async def ask(ctx: restate.WorkflowContext, agent: AgentObject, text: str) -> str:
    """Send ``text`` to ``agent``, in its conversation for this ticket (keyed by the workflow's id), and return its answer."""
    answer = await ctx.generic_call(agent.name, "chat", json.dumps(text).encode(), key=ctx.key())
    return json.loads(answer)


@triage.main()
async def run(ctx: restate.WorkflowContext, ticket: str) -> dict[str, str]:
    kind = await ask(ctx, classifier, ticket)
    draft = await ask(ctx, drafter, f"A {kind} ticket: {ticket}")
    ctx.set("draft", draft)

    # A person reviews the draft: the reply that goes out is theirs.
    reply = await ctx.promise("review", type_hint=str).value()
    return {"kind": kind, "reply": reply}


@triage.handler()
async def draft(ctx: restate.WorkflowSharedContext) -> str | None:
    """The drafted reply, once there is one."""
    return await ctx.get("draft", type_hint=str)


@triage.handler()
async def review(ctx: restate.WorkflowSharedContext, reply: str) -> None:
    """The reviewed reply, which goes out."""
    await ctx.promise("review", type_hint=str).resolve(reply)


if __name__ == "__main__":
    asyncio.run(restate.serve(restate.app([triage, classifier, drafter])))
