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
"""What a turn costs in commits of ``ctx.db`` when its tools are Restate tools, per framework.

A commit is an acknowledged round trip to Restate. Each framework's real adapter runs the same scripted turn (the
model's requests and tool calls below), once with plain tools and once with the same tools as Restate ``call`` tools,
over the in-process host that counts its syncs. The cost of the Restate tools is the difference.
"""

# pylint: disable=missing-function-docstring,missing-class-docstring

import json
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

import restate
from restate import sessions
from restate.sessions import Database
from restate.agents import call, tool
from restate.agents._tools import Tool
from restate.sessions.internal import invoke
from sessions_database import Backend  # pylint: disable=import-error
from sessions_object import Host, request  # pylint: disable=import-error


@pytest.fixture
def anyio_backend():
    return "asyncio"


EXTRA_COMMITS_PER_RESTATE_CALL = 3
"""What each Restate tool call costs in commits, over the same call of a plain tool. The call's record, in
``restate_agent_calls``, is committed: (1) before the call is made, (2) when it answered, and (3) when the record is
forgotten, after the framework stored the tool's result."""

REQUESTS = 4
TOOL_CALLS = 3
"""The turn: a tool call after each of the first ``TOOL_CALLS`` model requests, then the final answer."""

PLAIN_COMMITS = {
    "adk": 8,
    "langgraph agent": 8,
    "langgraph graph": 19,
    "openai": 8,
    "pydantic_ai": 8,
}
"""The commits of the second turn of a key, with plain tools, as measured. Each framework's own: the conversation
stores the user's message (1), each model request's response that calls a tool (3) and each tool's result (3), and
the last commit has the final response and the turn's answer (1). A graph of your own has more: LangGraph's
checkpointer commits the checkpoint of each step of the graph, with the writes of the step's nodes, and besides the
conversation's items (the copies of the 3 replies and 3 results, and the final reply with the answer) it has the input's
checkpoint, the write of the ``__start__`` task (which its checkpoint does not follow at once, so it is a commit of its
own), the 7 steps' (3 rounds of a model's reply and a tool's result, and the final reply), and the prune."""


# --- The callee, the tools, and the host that counts ------------------------------------------------

shipping = restate.VirtualObject("commits_shipping")


@shipping.handler()
async def status(ctx: restate.ObjectContext, order: str) -> str:
    return f"shipped {order}"


def result_of(order: str) -> str:
    """What both kinds of tool answer for an order: the callee's, as it is faked here."""
    return f"shipped {order}"


@tool(name="lookup")
async def plain_lookup(key: str, input: str) -> str:
    """The status of an order."""
    return result_of(input)


restated_lookup = call(status, name="lookup", description="The status of an order.")


TURN: list[str] = [""]
"""The turn that runs: its text, which its tool calls' ids are made of."""


def call_id(index: int) -> str:
    """A tool call's id, new for each turn: the same id is the same call, and each turn's calls are its own."""
    return f"call-{TURN[0]}-{index}"


def order_of(index: int) -> str:
    """The order that the ``index``-th tool call of the turn asks about: its own, in each turn."""
    return f"{TURN[0]}-o{index}"


def arguments_of(index: int) -> dict[str, str]:
    """The model's arguments of its ``index``-th tool call: both tools take them."""
    return {"key": "eu", "input": order_of(index)}


@dataclass
class Counting(Host):
    """A host that counts its syncs (each a commit), keeps what each committed, and answers the faked callee."""

    syncs: int = 0
    committed: list[dict[str, bytes]] = field(default_factory=list[dict[str, bytes]])

    async def sync(self) -> None:
        await super().sync()
        self.syncs += 1
        self.committed.append(dict(self.backend.stored))

    def generic_call(self, service, handler, arg, key=None, idempotency_key=None, headers=None, name=None):
        if handler == "status":
            self.answers[handler] = json.dumps(result_of(json.loads(arg))).encode()
        return super().generic_call(service, handler, arg, key, idempotency_key, headers, name)


@dataclass
class Measured:
    commits: int
    """The commits of the second turn."""
    committed: list[dict[str, bytes]]
    """What each of them committed (the store's entries)."""
    calls_made: int
    """The calls the Restate tools made."""
    answer: str = ""
    backend: Backend = field(default_factory=Backend)


Ask = Callable[[sessions.SessionContext, str, list[Tool]], Awaitable[str]]


async def measure(ask: Ask, restated: bool) -> Measured:
    """Run the turn twice on the same key, and return what the second cost."""
    asker = sessions.SessionObject("asker")

    @asker.handler
    async def run(ctx: sessions.SessionContext, text: str) -> str:
        return await ask(ctx, text, [restated_lookup if restated else plain_lookup])

    backend = Backend()

    async def turn(text: str) -> tuple[Counting, str]:
        TURN[0] = text
        host = Counting(backend, service="asker", answers={"publish": b'{"revision": 1}'})
        return host, await invoke(asker, "run", host, request(), text)

    await turn("first")
    host, answer = await turn("second")
    made = [c for c in host.calls if c[1] == "status"]
    return Measured(host.syncs, host.committed, len(made), answer, backend)


# --- Reading what a commit held -----------------------------------------------------------------


async def held(committed: dict[str, bytes]) -> tuple[str, list[dict[str, Any]]]:
    """What a commit held: all the stored tables but the calls' as text, and the calls' records."""
    reader = sessions.SessionObject("asker")

    @reader.handler
    async def read(ctx: sessions.SessionContext) -> tuple[str, list[dict[str, Any]]]:
        tables = [str(row["name"]) for row in await ctx.db.query("SELECT name FROM sqlite_master WHERE type = 'table'")]
        text = ""
        for table in tables:
            if table != "restate_agent_calls":
                text += repr(await ctx.db.query(f'SELECT * FROM "{table}"'))
        records: list[dict[str, Any]] = []
        if "restate_agent_calls" in tables:
            row = await ctx.db.get("SELECT calls FROM restate_agent_calls")
            records = json.loads(typing.cast(str, row["calls"])) if row else []
        return text, records

    copy = Backend()
    copy.stored = dict(committed)
    return await invoke(reader, "read", Host(copy, service="asker"), request())


async def assert_no_result_ahead_of_its_record(measured: Measured) -> None:
    """At no commit does the committed state hold a tool's real result while that call's record is not marked done.

    A call whose record was forgotten (after it was marked done) may hold its result."""
    done: set[int] = set()
    seen_result = [False] * TOOL_CALLS
    for committed in measured.committed:
        text, records = await held(committed)
        present = {i for i in range(TOOL_CALLS) for r in records if f'"{order_of(i)}"' in r["input"]}
        for i in range(TOOL_CALLS):
            for r in records:
                if f'"{order_of(i)}"' in r["input"] and r.get("done"):
                    done.add(i)
            if result_of(order_of(i)) in text:
                seen_result[i] = True
                assert i in done, f"the result of call {i} is committed while its record is not done: {records}"
                assert i not in present or i in done
    # The check is not vacuous: every result was committed, and each record was seen done.
    assert all(seen_result)
    assert done == set(range(TOOL_CALLS))


async def assert_costs(name: str, ask: Ask) -> None:
    plain = await measure(ask, restated=False)
    restated = await measure(ask, restated=True)
    assert plain.calls_made == 0 and restated.calls_made == TOOL_CALLS
    for measured in (plain, restated):
        assert measured.answer == "done"
        # Every tool call's result reached the transcript.
        text, _ = await held(measured.committed[-1])
        assert all(result_of(order_of(i)) in text for i in range(TOOL_CALLS))
    assert plain.commits == PLAIN_COMMITS[name]
    assert restated.commits == plain.commits + EXTRA_COMMITS_PER_RESTATE_CALL * TOOL_CALLS
    await assert_no_result_ahead_of_its_record(restated)


# --- Each framework's turn -----------------------------------------------------------------------


async def test_adk_turn():
    pytest.importorskip("google.adk")
    from collections.abc import AsyncGenerator  # pylint: disable=import-outside-toplevel

    from google.adk.models import BaseLlm  # pylint: disable=import-outside-toplevel
    from google.adk.models.llm_request import LlmRequest  # pylint: disable=import-outside-toplevel
    from google.adk.models.llm_response import LlmResponse  # pylint: disable=import-outside-toplevel
    from google.genai import types  # pylint: disable=import-outside-toplevel

    from restate.agents import adk  # pylint: disable=import-outside-toplevel

    class Scripted(BaseLlm):
        model: str = "scripted"
        asked: list[int] = []

        async def generate_content_async(
            self, llm_request: LlmRequest, stream: bool = False
        ) -> AsyncGenerator[LlmResponse, None]:
            n = len(self.asked)
            self.asked.append(n)
            if n < TOOL_CALLS:
                part = types.Part(function_call=types.FunctionCall(id=call_id(n), name="lookup", args=arguments_of(n)))
            else:
                part = types.Part(text="done")
            yield LlmResponse(content=types.Content(role="model", parts=[part]))

    async def ask(ctx: sessions.SessionContext, text: str, tools: list[Tool]) -> str:
        return await adk.prompt(ctx, text, model=Scripted(asked=[]), tools=[*tools])

    await assert_costs("adk", ask)


async def test_langgraph_agent_turn():
    pytest.importorskip("langgraph")
    from langchain_core.language_models import BaseChatModel  # pylint: disable=import-outside-toplevel
    from langchain_core.messages import AIMessage, BaseMessage  # pylint: disable=import-outside-toplevel
    from langchain_core.outputs import ChatGeneration, ChatResult  # pylint: disable=import-outside-toplevel

    from restate.agents import langgraph  # pylint: disable=import-outside-toplevel

    class Scripted(BaseChatModel):
        asked: list[int] = []

        @property
        def _llm_type(self) -> str:
            return "scripted"

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
            return self

        def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
            raise AssertionError("the requests are async")

        async def _agenerate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
            n = len(self.asked)
            self.asked.append(n)
            if n < TOOL_CALLS:
                reply = AIMessage("", tool_calls=[{"id": call_id(n), "name": "lookup", "args": arguments_of(n)}])
            else:
                reply = AIMessage("done")
            return ChatResult(generations=[ChatGeneration(message=reply)])

    async def ask(ctx: sessions.SessionContext, text: str, tools: list[Tool]) -> str:
        return await langgraph.prompt(ctx, text, model=Scripted(asked=[]), tools=[*tools])

    await assert_costs("langgraph agent", ask)


def langgraph_graph_ask() -> Ask:
    """The turn of a graph of your own: a model node and a tool node, as LangGraph's examples have them."""
    pytest.importorskip("langgraph")
    from langchain_core.messages import AIMessage  # pylint: disable=import-outside-toplevel
    from langgraph.graph import END, START, MessagesState, StateGraph  # pylint: disable=import-outside-toplevel
    from langgraph.prebuilt import ToolNode  # pylint: disable=import-outside-toplevel

    from restate.agents import langgraph  # pylint: disable=import-outside-toplevel

    async def ask(ctx: sessions.SessionContext, text: str, tools: list[Tool]) -> str:
        asked: list[int] = []

        async def model(state: MessagesState) -> dict[str, Any]:
            n = len(asked)
            asked.append(n)
            if n < TOOL_CALLS:
                reply = AIMessage("", tool_calls=[{"id": call_id(n), "name": "lookup", "args": arguments_of(n)}])
            else:
                reply = AIMessage("done")
            return {"messages": [reply]}

        def route(state: MessagesState) -> str:
            last = state["messages"][-1]
            return "tools" if getattr(last, "tool_calls", None) else END

        builder = StateGraph(MessagesState)
        builder.add_node("model", model)
        builder.add_node("tools", ToolNode(langgraph.tools([*tools])))
        builder.add_edge(START, "model")
        builder.add_conditional_edges("model", route, ["tools", END])
        builder.add_edge("tools", "model")
        return await langgraph.prompt(ctx, text, graph=builder)

    return ask


async def test_langgraph_graph_turn():
    pytest.importorskip("langgraph")
    await assert_costs("langgraph graph", langgraph_graph_ask())


async def test_langgraph_graph_checkpointer_commits(monkeypatch: pytest.MonkeyPatch):
    """The checkpointer's commits, in order, of the second turn: a step's writes are in its checkpoint's commit."""
    pytest.importorskip("langgraph")
    from restate.agents.langgraph import SqlCheckpointSaver, _graph  # pylint: disable=import-outside-toplevel
    from transactions_spy import Spy  # pylint: disable=import-outside-toplevel,import-error

    spies: list[Spy] = []

    class Recording(SqlCheckpointSaver):
        def __init__(self, db: Database) -> None:
            spies.append(Spy(db))
            super().__init__(spies[-1].database)

    monkeypatch.setattr(_graph, "SqlCheckpointSaver", Recording)
    measured = await measure(langgraph_graph_ask(), restated=False)
    assert len(spies) == 2  # one saver for each turn
    put = ["restate_langgraph_channel_values", "restate_langgraph_checkpoints"]
    step = ["restate_langgraph_writes", *put]
    prune = ["restate_langgraph_checkpoints", "restate_langgraph_writes", "restate_langgraph_channel_values"]
    # The input's checkpoint, the write of LangGraph's ``__start__`` task (which its checkpoint does not follow at
    # once), the checkpoint of the first step; then each step of the other 7 (3 rounds of a model's reply and a tool's
    # result, and the final reply), a commit with its writes; and the thread pruned to its latest checkpoint.
    assert spies[1].tables == [
        put,
        ["restate_langgraph_writes"],
        put,
        *[step] * 7,
        prune,
    ]
    # The commits that are not the checkpointer's are the conversation's: the user's message, the copies of the 3
    # replies with tool calls and the 3 results (each after its checkpoint: they run beside the next step, whose
    # commit may come first), and the final reply with the answer, last.
    assert measured.commits - len(spies[1].commits) == 8
    assert measured.commits == PLAIN_COMMITS["langgraph graph"]


async def test_openai_turn():
    pytest.importorskip("agents")
    from agents import Model, ModelResponse, Usage, set_tracing_disabled  # pylint: disable=import-outside-toplevel
    from openai.types.responses import (  # pylint: disable=import-outside-toplevel
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )

    from restate.agents import openai  # pylint: disable=import-outside-toplevel

    set_tracing_disabled(True)

    class Scripted(Model):
        def __init__(self) -> None:
            self.asked = 0

        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            n = self.asked
            self.asked += 1
            output: Any
            if n < TOOL_CALLS:
                output = ResponseFunctionToolCall(
                    type="function_call", call_id=call_id(n), name="lookup", arguments=json.dumps(arguments_of(n))
                )
            else:
                text: list[Any] = [ResponseOutputText(type="output_text", text="done", annotations=[])]
                output = ResponseOutputMessage(
                    id="msg", type="message", role="assistant", status="completed", content=text
                )
            return ModelResponse(output=[output], usage=Usage(), response_id=None)

        def stream_response(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

    async def ask(ctx: sessions.SessionContext, text: str, tools: list[Tool]) -> str:
        return await openai.prompt(ctx, text, model=Scripted(), tools=[*tools])

    await assert_costs("openai", ask)


async def test_pydantic_ai_turn():
    pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import (  # pylint: disable=import-outside-toplevel
        ModelMessage,
        ModelResponse,
        TextPart,
        ToolCallPart,
    )
    from pydantic_ai.models.function import AgentInfo, FunctionModel  # pylint: disable=import-outside-toplevel

    from restate.agents import pydantic_ai  # pylint: disable=import-outside-toplevel

    async def ask(ctx: sessions.SessionContext, text: str, tools: list[Tool]) -> str:
        asked: list[int] = []

        def scripted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            n = len(asked)
            asked.append(n)
            if n < TOOL_CALLS:
                return ModelResponse(parts=[ToolCallPart("lookup", arguments_of(n), tool_call_id=call_id(n))])
            return ModelResponse(parts=[TextPart("done")])

        return await pydantic_ai.prompt(ctx, text, model=FunctionModel(scripted), tools=[*tools])

    await assert_costs("pydantic_ai", ask)
