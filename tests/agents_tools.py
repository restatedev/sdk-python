#
#  Copyright (c) 2023-2026 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""Tools: from functions, and the tools that are Restate itself, over an in-memory copy of an agent's state."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import json
import typing
from typing import Any

import pydantic
import pytest

import restate
from restate import sessions
from restate.sessions import internal
from restate.agents._common import ToolError
from restate.agents._object import AgentObject
from restate.agents._tools import Tool, ToolCall, call, subagent, tool, wait_for_signal, workflow
from restate.context import Request
from sessions_database import Backend  # pylint: disable=import-error
from sessions_object import Host, pay, request  # pylint: disable=import-error


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- The targets the tools call -------------------------------------------------------------------


class Query(pydantic.BaseModel):
    order: str


orders = restate.VirtualObject("orders")


@orders.handler()
async def status(ctx: restate.ObjectContext, query: Query) -> dict[str, str]:
    return {"order": query.order, "status": "shipped"}


helper = AgentObject("helper", description="Looks things up")


# --- An agent whose handler runs a tool, for a tool call ------------------------------------------

runner = AgentObject("runner")
tools: dict[str, Tool] = {}


@runner.handler
async def use(ctx: sessions.SessionContext, step: dict[str, Any]) -> str:
    return await tools[step["tool"]].run(step["args"], ToolCall(ctx, step["call"]))


async def run_tool(host: Host, name: str, args: dict[str, Any], call_id: str, req: Request | None = None) -> str:
    return await internal.invoke(runner, "use", host, req or request(), {"tool": name, "args": args, "call": call_id})


# --- tool -----------------------------------------------------------------------------------------


@tool
async def get_weather(ctx: sessions.SessionContext, call_id: str, city: str, unit: str = "C") -> str:
    """The current weather in a city."""
    if city == "Atlantis":
        raise restate.TerminalError("no such city")
    if city == "Crash":
        raise RuntimeError("the weather API is down")
    return f"Sunny, 21°{unit} in {city} ({ctx.key}, {call_id})"


def test_a_tool_takes_its_input_from_the_function_and_its_description_from_the_docstring():
    assert get_weather.name == "get_weather"
    assert get_weather.description == "The current weather in a city."
    assert set(get_weather.input_schema["properties"]) == {"city", "unit"}
    assert get_weather.input_schema["required"] == ["city"]


async def test_a_tool_answers_with_text_and_its_errors_go_to_the_model_or_fail_the_attempt():
    tools["weather"] = get_weather
    host = Host(Backend(), service="runner", key="alice")
    assert await run_tool(host, "weather", {"city": "Lisbon"}, "c1") == "Sunny, 21°C in Lisbon (alice, c1)"
    bad = await run_tool(host, "weather", {"town": "Lisbon"}, "c2")
    assert bad.startswith("Error: the arguments do not fit the tool's input")
    assert await run_tool(host, "weather", {"city": "Atlantis"}, "c3") == "Error: no such city"
    with pytest.raises(ToolError, match="the tool get_weather failed: the weather API is down"):
        await run_tool(host, "weather", {"city": "Crash"}, "c4")


# --- call -----------------------------------------------------------------------------------------


async def test_call_lets_the_model_choose_the_key_and_makes_the_call_once_per_tool_call():
    tools["status"] = call(status, description="The status of an order.")
    assert tools["status"].name == "orders_status"
    schema = tools["status"].input_schema
    assert set(schema["properties"]) == {"key", "order"} and set(schema["required"]) == {"key", "order"}

    host = Host(Backend(), service="runner", key="alice", answers={"status": b'{"order": "o1", "status": "shipped"}'})
    req = request()
    answer = await run_tool(host, "status", {"key": "eu", "order": "o1"}, "t1", req)
    assert json.loads(answer) == {"order": "o1", "status": "shipped"}
    [(service, handler, arg, key, idempotency_key)] = host.calls
    assert (service, handler, key, idempotency_key) == ("orders", "status", "eu", f"{req.id}:t1")
    assert json.loads(arg) == {"order": "o1"}

    # Another attempt of the invocation, whose framework makes the call again under a new id: the same call.
    await run_tool(host, "status", {"key": "eu", "order": "o1"}, "t2", req)
    assert host.calls[-1][4] == f"{req.id}:t1"


# --- workflow -------------------------------------------------------------------------------------


async def test_workflow_sends_to_the_workflow_and_waits_for_the_run_the_send_started():
    """The tool sends to the workflow, and attaches to the invocation of the send. Run again (after a crash), it sends
    again, which Restate starts nothing for (the same run, of the same id), and waits for that run again."""
    tools["pay"] = workflow(pay, description="Pay an amount out.")
    host = Host(Backend(), service="runner", key="alice", answers={"inv_payouts/alice-c1": b'"paid 30"'})
    req = request()
    assert await run_tool(host, "pay", {"input": 30}, "c1", req) == "paid 30"
    assert await run_tool(host, "pay", {"input": 30}, "c1", req) == "paid 30"
    assert [(s.service, s.handler, s.arg, s.key) for s in host.sends] == [("payouts", "pay", b"30", "alice-c1")] * 2
    assert host.attached == ["inv_payouts/alice-c1"] * 2
    assert host.calls == []


# --- signal and subagent --------------------------------------------------------------------------


class Refund(pydantic.BaseModel):
    order: str
    amount: float


async def test_signal_waits_at_the_companion_of_the_agent_with_the_tool_calls_key():
    tools["approval"] = wait_for_signal("approval", description="Wait for a manager's approval.", input=Refund)
    assert tools["approval"].name == "wait_for_approval"
    assert set(tools["approval"].input_schema["properties"]) == {"order", "amount"}
    host = Host(Backend(), service="runner", key="alice", answers={"wait": b'"approved"'})
    req = request()
    assert await run_tool(host, "approval", {"order": "o1", "amount": 5}, "w1", req) == "approved"
    [(service, handler, arg, key, idempotency_key)] = host.calls
    assert (service, handler, key, idempotency_key) == ("runner.agentprebuild.restate", "wait", "alice", f"{req.id}:w1")
    wait = typing.cast(dict[str, Any], json.loads(arg))
    assert wait == {"id": f"{req.id}:w1", "name": "approval", "input": {"order": "o1", "amount": 5}}


async def test_subagent_hands_a_task_to_the_agent_keyed_by_this_key_and_the_tasks_name():
    tools["helper"] = subagent(helper)
    assert "Hand a task to the helper agent (Looks things up)" in tools["helper"].description
    host = Host(Backend(), service="runner", key="alice", answers={"chat": b'"found it"'})
    assert await run_tool(host, "helper", {"name": "lookup", "task": "Find it"}, "s1") == "found it"
    [(service, handler, arg, key, _)] = host.calls
    assert (service, handler, key, json.loads(arg)) == ("helper", "chat", "alice.lookup", "Find it")
    bad = await run_tool(host, "helper", {"name": "Not A Name", "task": "x"}, "s2")
    assert bad.startswith("Error: the arguments do not fit")


async def test_a_stopped_turn_reports_a_call_that_returned_as_done_not_cancelled():
    from restate.agents._calls import call_of, outcomes  # pylint: disable=import-outside-toplevel
    from restate.agents._common import (  # pylint: disable=import-outside-toplevel
        CANCELLED,
        RETURNED_UNRECORDED,
        cut_off_result,
    )

    recorded = sessions.SessionObject("stopped")
    seen: dict[str, str] = {}
    ids = ("call-refund", "call-status", "call-charge", "call-none")

    @recorded.handler
    async def turn(ctx: sessions.SessionContext) -> None:
        refund = await call_of(ctx, "refund", {"order": "o1"}, "call-refund")
        await refund.answered()
        await call_of(ctx, "status", {"order": "o1"}, "call-status")  # made, but not returned
        charge = await call_of(ctx, "charge", {"order": "o1"}, "call-charge")
        await charge.answered(failed="Error: card declined")
        returned = await outcomes(ctx, ids)
        seen.update({call_id: cut_off_result(returned, call_id, CANCELLED) for call_id in ids})

    await internal.invoke(recorded, "turn", Host(Backend(), service="stopped"), request())
    assert seen == {
        "call-refund": RETURNED_UNRECORDED,  # it happened: the model must not take it for undone
        "call-status": CANCELLED,
        "call-charge": "Error: card declined",
        "call-none": CANCELLED,
    }


# --- A call through a crash: kept until its result is stored, then forgotten ----------------------


async def test_a_returned_call_is_kept_until_its_result_is_stored_and_forgotten_once_it_is():
    """A crash after a call returned and before its result is stored, and the model asking again under a new id.

    The call's record outlives the turn's publishes until the conversation
    has its result: the retry, under a new tool call id, reaches the same
    Restate call (the same idempotency key) rather than make it again. Once
    the result is stored, the record is forgotten.
    """
    from agents_conversation import ITEMS, PUBLISHED  # pylint: disable=import-error,import-outside-toplevel

    from restate.agents._conversation import Conversation, answer_once  # pylint: disable=import-outside-toplevel
    from restate.agents._turn import tool_call_ended, tool_call_started  # pylint: disable=import-outside-toplevel

    lookup = call(status, description="The status of an order.")
    args = {"key": "eu", "order": "o1"}
    attempts: list[str] = []
    obj = sessions.SessionObject("crashed_caller")

    @obj.handler
    async def ask(ctx: sessions.SessionContext, text: str) -> str:
        async def run(conversation: Conversation) -> str:
            attempts.append("ran")
            if len(attempts) == 1:
                calls = [{"id": "t1", "name": lookup.name, "input": args}]
                await conversation.turn.append_reply({"role": "assistant", "text": "", "calls": calls})
                await tool_call_started(ctx)
                await lookup.run(args, ToolCall(ctx, "t1"))
                await tool_call_ended(ctx)  # a publish: the calls whose results are stored are forgotten
                raise RuntimeError("the process crashed before the result was stored")
            # The framework drops the cut-off reply, and the model makes the call again under a new id.
            [cut] = [entry.reply for entry in conversation.turn.items if entry.calls]
            assert cut is not None
            await conversation.turn.drop(cut)
            calls = [{"id": "t2", "name": lookup.name, "input": args}]
            await conversation.turn.append_reply({"role": "assistant", "text": "", "calls": calls})
            await tool_call_started(ctx)
            output = await lookup.run(args, ToolCall(ctx, "t2"))
            await conversation.turn.append({"result": "t2", "output": output})
            await tool_call_ended(ctx)
            await conversation.turn.append_reply({"role": "assistant", "text": "shipped"})
            return "shipped"

        return await answer_once(ctx, text, ITEMS, run)

    @obj.handler(name="records")
    async def records(ctx: sessions.SessionContext) -> list[Any]:
        row = await ctx.db.get("SELECT calls FROM restate_agent_calls")
        return [] if row is None else json.loads(typing.cast(str, row["calls"]))

    backend, req = Backend(), request()
    answers = {**PUBLISHED, "status": b'{"order": "o1", "status": "shipped"}'}
    first = Host(backend, service="crashed_caller", answers=answers)
    with pytest.raises(RuntimeError, match="crashed before the result"):
        await internal.invoke(obj, "ask", first, req, "where is o1?")
    second = Host(backend, service="crashed_caller", answers=answers)
    assert await internal.invoke(obj, "ask", second, req, "where is o1?") == "shipped"

    # The retry reached the same Restate call: the idempotency key of the first tool call id.
    made = [c for c in [*first.calls, *second.calls] if c[1] == "status"]
    assert [c[4] for c in made] == [f"{req.id}:t1", f"{req.id}:t1"]
    # Its result is stored, and so its record is forgotten.
    assert await internal.invoke(obj, "records", Host(backend, service="crashed_caller"), req) == []
