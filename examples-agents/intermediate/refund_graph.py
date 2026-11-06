"""
A refund desk you chat with, as a LangGraph graph of your own whose steps are
Restate's: it asks a clerk agent to read the request, asks a policy agent
about it, waits for a manager's approval, runs the payout workflow, and
answers. Each step happens once, even if the process crashes in the middle;
the wait for approval holds no process, for as long as it takes::

    OPENAI_API_KEY=... uv run --extra langgraph --extra serve --with langchain-openai examples-agents/intermediate/refund_graph.py
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080
    curl localhost:8080/refunds/alice/chat -d 'Order 103 arrived broken, I want my $30 back'   # waits for the approval

Then, in another terminal, see what the desk waits for, and approve it::

    curl localhost:8080/refunds/alice/pending
    curl localhost:8080/refunds/alice/signal --json '{"name": "approval", "value": "approved"}'

Any other value rejects the refund. ``python -m restate.agents chat refunds/alice``
shows the wait for approval as it comes, and the answer.
"""

import asyncio
from typing import Any, NotRequired

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from pydantic import BaseModel

import restate
from restate.agents import langgraph
from restate.agents.langgraph import nodes


class Refund(BaseModel):
    order: str
    amount: float


class Desk(MessagesState):
    """The graph's state: the conversation (LangGraph's messages), and the refund it works on."""

    refund: NotRequired[dict[str, Any]]
    policy: NotRequired[str]
    approval: NotRequired[str]
    receipt: NotRequired[str]


# The payout, as a workflow: it runs once per order.
payouts = restate.Workflow("payouts")


@payouts.main()
async def pay(_ctx: restate.WorkflowContext, refund: Refund) -> str:
    return f"Refunded ${refund.amount:g} for order {refund.order}."


# Two agents the graph asks: one reads the request into a Refund, one knows the policy.
clerk = langgraph.agent("clerk", system="Read the refund request: which order, and how much.", output=Refund)
policy = langgraph.agent(
    "policy",
    system="You know the refund policy: refunds up to $50 within 30 days. Say in one sentence whether a refund fits.",
)


def asked(state: Desk) -> str:
    return str(state["messages"][-1].text)


def about(state: Desk) -> str:
    refund = state.get("refund", {})
    return f"A refund of ${float(refund['amount']):g} for order {refund['order']}."


def answer(state: Desk) -> dict[str, list[AIMessage]]:
    if state.get("approval") != "approved":
        return {"messages": [AIMessage(f"The refund was not approved: {about(state)}")]}
    return {"messages": [AIMessage(state.get("receipt", ""))]}


def approved(state: Desk) -> str:
    return "pay" if state.get("approval") == "approved" else "answer"


graph = StateGraph(Desk)
graph.add_node("read", nodes.subagent(clerk, message=asked, output="refund"))
graph.add_node("check", nodes.subagent(policy, message=about, output="policy"))
graph.add_node(
    "approve",
    nodes.wait_for_signal(
        "approval", input=lambda s: {"refund": s["refund"], "policy": s["policy"]}, output="approval"
    ),
)
graph.add_node(
    "pay",
    nodes.workflow(pay, input=lambda s: s["refund"], key=lambda s: s["refund"]["order"], output="receipt"),
)
graph.add_node("answer", answer)
graph.add_edge(START, "read")
graph.add_edge("read", "check")
graph.add_edge("check", "approve")
graph.add_conditional_edges("approve", approved, ["pay", "answer"])
graph.add_edge("pay", "answer")
graph.add_edge("answer", END)

# An agent whose chat runs the graph: each key's database keeps its checkpoints and its conversation.
refunds = langgraph.agent("refunds", graph=graph, description="A refund desk that runs a LangGraph graph.")


if __name__ == "__main__":
    asyncio.run(restate.serve(restate.app([payouts, clerk, policy, refunds])))
