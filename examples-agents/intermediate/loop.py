"""
An agent loop of your own, with no agent framework: ask the model, run the
tools it calls, and ask it again with their results, until it answers. Here
the model is OpenAI's, through its Responses API; any model that calls tools
works the same way::

    OPENAI_API_KEY=... uv run --extra serve --with openai examples-agents/intermediate/loop.py
    npx @restatedev/restate-server                                                  # Restate, in another terminal
    npx @restatedev/restate -y deployments register --force http://localhost:9080
    curl localhost:8080/refund_loop/alice/chat -d 'Order 103 arrived broken, can I get my $30 back?'

The agent waits for a manager to approve the refund. In another terminal,
approve it::

    curl localhost:8080/refund_loop/alice/signal --json '{"name": "approval", "value": "approved"}'

Or talk with the agent, and approve, from the agents' command line:
``python -m restate.agents chat refund_loop/alice``, then ``/signal approval approved``.

The loop is plain Python, and Restate makes it durable:

- The conversation is in the key's database, and the model gets its history
  (``conversation.history()``): earlier turns as messages and answers, and the
  latest turns whole. Each of the model's replies is
  stored before its tools run, and each tool's result as it comes back.
- The tools a reply calls run at once, and each result is stored as it comes.
- A crash, or a lost connection, retries the turn. The loop starts again from
  what is stored: it runs the tool calls that have no result yet (with the same
  call ids), then asks the model, so a tool that already answered does not run
  again; and a final reply already stored is the answer.
- ``answer_once`` answers each message once: a retried message returns its
  stored answer. ``stop`` cancels a turn, even mid-tool.
- The tools are ``restate.agents`` tools: a function of your own, or Restate
  itself (here, a durable wait for a signal; also calls, sends, workflows and
  subagents).
- The agent gets the views of every agent: ``messages``, ``status``,
  ``pending``, ``signal``, ``stop`` and ``clear``.
"""

import asyncio
import functools
import json
import os
from collections.abc import Awaitable, Iterable, Sequence
from typing import Any

from pydantic import BaseModel

import restate
from restate import sessions
from restate.agents import AgentObject, ToolCall, tool, wait_for_signal
from restate.agents.internal import Call, Conversation, Parsed, Result, answer_once, run_tool

MODEL = os.environ.get("RESTATE_AGENT_MODEL", "openai/gpt-6-sol").removeprefix("openai/")
SYSTEM = "You handle refunds. Look the order up, have a manager approve the refund, then refund it. Answer briefly."


# --- The tools --------------------------------------------------------------------------------------


class Refund(BaseModel):
    order: str
    amount: float


@tool
def order_status(order: str) -> str:
    """Look an order up."""
    return f"Order {order} was delivered yesterday."


approval = wait_for_signal("approval", description="Wait for a manager to approve a refund.", input=Refund)


@tool
def refund(order: str, amount: float) -> str:
    """Refund an order (once approved)."""
    return f"Refunded ${amount:g} for order {order}."


TOOLS = {t.name: t for t in (order_status, approval, refund)}
SCHEMAS = [
    {"type": "function", "name": t.name, "description": t.description, "parameters": t.input_schema, "strict": False}
    for t in TOOLS.values()
]


# --- The model --------------------------------------------------------------------------------------


@functools.cache
def openai_client() -> Any:
    import openai  # pylint: disable=import-outside-toplevel

    return openai.AsyncOpenAI()


async def complete(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ask the model, and return its reply: its items, such as its reasoning, its text, and the tools it calls."""
    response = await openai_client().responses.create(
        model=MODEL,
        instructions=SYSTEM,
        input=items,
        tools=SCHEMAS,
        # The conversation is in Restate, not OpenAI's: each reasoning item carries its own (encrypted) content.
        store=False,
        include=["reasoning.encrypted_content"],
    )
    return [item.model_dump(exclude_none=True) for item in response.output]


# --- The conversation's items: the Responses API's ------------------------------------------------


class Messages:
    """How the conversation stores, reads and makes the loop's items (``restate.agents.internal.Items``)."""

    def dump(self, item: dict[str, Any]) -> str:
        return json.dumps(item)

    def load(self, stored: str) -> dict[str, Any]:
        return json.loads(stored)

    def prompt(self, text: str) -> dict[str, Any]:
        return {"role": "user", "content": text}

    def reply(self, text: str, agent: str | None = None) -> dict[str, Any]:
        return {"role": "assistant", "content": text}

    def results(self, results: Sequence[tuple[Call, str]]) -> list[dict[str, Any]]:
        return [result(call.id, text) for call, text in results]

    def read(self, item: dict[str, Any]) -> Parsed:
        match item.get("type", "message"):
            case "message":
                return Parsed((item["role"], _text(item["content"])))
            case "function_call":
                return Parsed(calls=[Call(item["call_id"], item["name"], _arguments(item["arguments"]))])
            case "function_call_output":
                return Parsed(results=[Result(item["call_id"], item["output"])])
            case _:  # the model's reasoning, say
                return Parsed()


def result(call_id: str, output: str) -> dict[str, Any]:
    """The item that gives tool call ``call_id`` its output."""
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def _text(content: str | list[dict[str, Any]]) -> str:
    """The text of a message: a string, or its parts' (the model's ``output_text``)."""
    return content if isinstance(content, str) else "".join(part.get("text", "") for part in content)


def _arguments(arguments: str) -> Any:
    try:
        return json.loads(arguments)
    except ValueError:
        return {}  # the tool then tells the model its arguments do not fit


MESSAGES = Messages()


async def all_of(runs: Iterable[Awaitable[None]]) -> None:
    """Run ``runs`` at once; if one fails, cancel the others, and fail with its error once they have unwound."""
    tasks = [asyncio.ensure_future(r) for r in runs]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)  # let them unwind, before the turn closes
        raise


# --- The loop ---------------------------------------------------------------------------------------


async def answer(ctx: sessions.SessionContext, text: str) -> str:
    """Answer a message: the model, and the tools it calls, until it answers with no more calls."""

    async def run(conversation: Conversation) -> str:
        async def run_call(call: Call) -> None:
            found = TOOLS.get(call.name)
            output = (
                await run_tool(found, call.input, ToolCall(ctx, call.id))
                if found is not None
                else f"Error: there is no tool named {call.name}"
            )
            await conversation.turn.append(result(call.id, output))  # stored as soon as it comes

        if (final := conversation.turn.final_reply()) is not None:
            # A crash came after the model's final reply was stored, before its answer was: that is the answer.
            return final
        while True:
            # The tool calls with no result yet (the last reply's, or those a crash cut off), all at once.
            await all_of(run_call(call) for call in conversation.turn.open_calls())
            reply = await complete(conversation.history())
            await conversation.turn.append_reply(*reply)
            if not any(item["type"] == "function_call" for item in reply):
                return conversation.turn.final_reply() or ""

    return await answer_once(ctx, text, MESSAGES, run)


refund_loop = AgentObject("refund_loop")
refund_loop.chat(answer, items=MESSAGES)


if __name__ == "__main__":
    asyncio.run(restate.serve(restate.app([refund_loop])))
