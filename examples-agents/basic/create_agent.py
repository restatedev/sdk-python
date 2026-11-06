"""
LangChain's ``create_agent`` as a Restate agent: an order desk, each
conversation in its key's database. Its tool is Restate's: a call to the
``orders`` service, made once per tool call even if the process crashes
mid-turn::

    OPENAI_API_KEY=... uv run --extra langgraph --extra langchain --extra serve --with langchain-openai examples-agents/basic/create_agent.py
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080
    curl localhost:8080/desk/alice/chat -d 'Where is my order 103?'
    curl localhost:8080/desk/alice/messages --json '{}'                             # the conversation

A graph of your own works the same way: ``langgraph.agent("desk", graph=...)``
takes any LangGraph graph built on ``MessagesState``.
"""

import asyncio

from langchain.agents import create_agent

import restate
from restate.agents import call, langgraph

orders = restate.Service("orders")


@orders.handler()
async def status(_ctx: restate.Context, order: str) -> str:
    return f"Order {order} was delivered yesterday."


graph = create_agent(
    "openai:gpt-6-sol",
    tools=langgraph.tools([call(status, description="The status of an order, by its number.")]),
    system_prompt="You help customers with their orders. Answer briefly.",
)
desk = langgraph.agent("desk", graph=graph)


if __name__ == "__main__":
    asyncio.run(restate.serve(restate.app([orders, desk])))
