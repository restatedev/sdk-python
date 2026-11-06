"""
A LangGraph graph of your own, whose steps are Restate's: a refund desk asks a
policy agent about the refund, waits for a manager's approval, and runs the
payout workflow. Each step happens once, even if the process crashes in the
middle; the wait for approval holds no process, for as long as it takes::

    OPENAI_API_KEY=... uv run --extra langgraph --extra serve --with langchain-openai examples-agents/intermediate/refund_graph.py
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080
    curl localhost:8080/refunds/alice/request --json '{"order": "103", "amount": 30}'   # waits for the approval

Then, in another terminal, see what the desk waits for, and approve it::

    curl localhost:8080/refunds/alice/pending
    curl localhost:8080/refunds/alice/signal --json '{"name": "approval", "value": "approved"}'

Any other value rejects the refund, and the graph ends without the payout.
"""

import asyncio
from typing import Any, Required, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

import restate
from restate import sessions
from restate.agents import AgentObject, langgraph
from restate.agents.langgraph import SqlCheckpointSaver, nodes


class Refund(TypedDict):
    order: str
    amount: float


class State(TypedDict, total=False):
    refund: Required[Refund]
    policy: str
    approval: str
    receipt: str


# The payout, as a workflow: it runs once per refund.
payouts = restate.Workflow("payouts")


@payouts.main()
async def pay(_ctx: restate.WorkflowContext, refund: Refund) -> str:
    return f"Refunded ${refund['amount']:g} for order {refund['order']}."


# An agent that knows the refund policy; the graph asks it about each refund.
policy = langgraph.agent(
    "policy",
    system="You know the refund policy: refunds up to $50 within 30 days. Say in one sentence whether a refund fits.",
)


def approved(state: State) -> str:
    return "pay" if state.get("approval") == "approved" else END


graph = StateGraph(State)
graph.add_node(
    "check",
    nodes.subagent(
        policy,
        message=lambda s: f"A refund of ${s['refund']['amount']:g} for order {s['refund']['order']}.",
        output="policy",
    ),
)
graph.add_node("approve", nodes.wait_for_signal("approval", input=lambda s: s, output="approval"))
graph.add_node(
    "pay", nodes.workflow(pay, input=lambda s: s["refund"], key=lambda s: s["refund"]["order"], output="receipt")
)
graph.add_edge(START, "check")
graph.add_edge("check", "approve")
graph.add_conditional_edges("approve", approved, ["pay", END])
graph.add_edge("pay", END)

# The graph runs in a handler of an agent: its key's database keeps the graph's checkpoints, and its `pending` and
# `signal` views show and resolve the waits.
refunds = AgentObject("refunds", description="A refund desk that runs a LangGraph graph.")


@refunds.handler()
async def request(ctx: sessions.SessionContext, refund: Refund) -> State:
    # Untyped: LangGraph types a run's context as a state-like value, and the context here is the handler's.
    app: Any = graph.compile(checkpointer=SqlCheckpointSaver(ctx.db))
    config: RunnableConfig = {"configurable": {"thread_id": refund["order"]}}
    # A retried attempt (after a crash) resumes the run it cut off, rather than start another.
    resuming = (await app.aget_state(config)).next
    state: State | None = None if resuming else {"refund": refund}
    # The nodes make their Restate calls with the handler's context, and need each step stored before it runs.
    return cast(State, await app.ainvoke(state, config, context=ctx, durability="sync"))


if __name__ == "__main__":
    asyncio.run(restate.serve(restate.app([payouts, policy, refunds])))
