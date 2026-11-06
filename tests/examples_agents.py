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
"""The examples of examples-agents/, each with the commands its comment lists, against a real Restate server."""

# pylint: disable=missing-function-docstring

import asyncio
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import typing
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from types import ModuleType
from typing import Any

import httpx2
import pytest

import restate
from restate.harness import RestateContainer, find_free_port


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

EXAMPLES = pathlib.Path(__file__).parent.parent / "examples-agents"
SERVABLE = (restate.Service, restate.VirtualObject, restate.Workflow)


def load(path: str) -> ModuleType:
    """An example's module, from its file: what it serves, without serving it."""
    spec = importlib.util.spec_from_file_location(f"example_{uuid.uuid4().hex}", EXAMPLES / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Ingress:
    """Restate's ingress, as the examples' curl commands call it."""

    def __init__(self, url: str) -> None:
        self.url = url

    async def post(self, path: str, body: object = None) -> httpx2.Response:
        async with httpx2.AsyncClient(timeout=60) as client:
            return await client.post(f"{self.url}/{path}", json=body)


@asynccontextmanager
async def serving(path: str, setup: Callable[[ModuleType], None] | None = None) -> AsyncGenerator[Ingress, None]:
    """The services, objects and workflows of the example at ``path``, served to a Restate server of their own.

    ``setup``, if given, gets the example's module first: to script its model, say.
    """
    module = load(path)
    if setup is not None:
        setup(module)
    objects = [value for value in vars(module).values() if isinstance(value, SERVABLE)]
    async with restate.create_test_harness(
        restate.app(objects), restate_image="ghcr.io/restatedev/restate:latest"
    ) as harness:
        yield Ingress(harness.ingress_url)


async def test_01_object():
    async with serving("tutorial/01_object.py") as ingress:
        assert (await ingress.post("wallet/alice/hello")).json() == "Hello, alice!"


async def test_02_database():
    async with serving("tutorial/02_database.py") as ingress:
        assert (await ingress.post("wallet/alice/deposit", 10)).json() == 10
        assert (await ingress.post("wallet/alice/deposit", 5)).json() == 15
        assert (await ingress.post("wallet/bob/deposit", 7)).json() == 7


async def test_03_views():
    async with serving("tutorial/03_views.py") as ingress:
        await ingress.post("wallet/alice/deposit", 20)
        assert (await ingress.post("wallet/alice/balance")).json() == 20
        assert (await ingress.post("wallet/alice/history")).json() == [20]


async def test_04_schema():
    async with serving("tutorial/04_migrations.py") as ingress:
        await ingress.post("wallet/alice/deposit", {"amount": 50, "note": "birthday"})
        assert (await ingress.post("wallet/alice/history")).json() == [{"amount": 50, "note": "birthday"}]


async def test_05_transaction():
    async with serving("tutorial/05_transaction.py") as ingress:
        await ingress.post("wallet/alice/deposit", {"amount": 30, "note": "salary"})
        assert (await ingress.post("wallet/alice/spend", {"amount": 12, "note": "lunch"})).json() == 18
        boat = await ingress.post("wallet/alice/spend", {"amount": 1000, "note": "a boat"})
        assert boat.status_code == 500 and "Not enough money" in boat.text
        history = (await ingress.post("wallet/alice/history")).json()
        assert history == [{"amount": 30, "note": "salary"}, {"amount": -12, "note": "lunch"}]


async def test_06_retries():
    async with serving("tutorial/06_retries.py") as ingress:
        refund = {"id": "d1", "amount": 10, "note": "refund"}
        # Counted once, though the first attempt failed after the insert; and once when sent again.
        assert (await ingress.post("wallet/alice/deposit", refund)).json() == 10
        assert (await ingress.post("wallet/alice/deposit", refund)).json() == 10


async def test_07_calls():
    async with serving("tutorial/07_calls.py") as ingress:
        assert (await ingress.post("wallet/alice/claim", "ace")).json() == "alice is now @ace"
        taken = await ingress.post("wallet/bob/claim", "ace")
        assert taken.status_code == 500 and "@ace belongs to alice" in taken.text
        assert (await ingress.post("handle/ace/owner")).json() == "alice"


async def test_08_sends():
    async with serving("tutorial/08_sends.py") as ingress:
        await ingress.post("wallet/alice/deposit", {"id": "d2", "amount": 100})
        assert (await ingress.post("wallet/alice/transfer", {"to": "bob", "amount": 30})).json() == 70
        history: list[typing.Any] = []
        for _ in range(100):
            history = (await ingress.post("wallet/bob/history")).json()
            if history:
                break
            await asyncio.sleep(0.05)
        assert history == [{"amount": 30, "note": "from alice"}]
        too_much = await ingress.post("wallet/alice/transfer", {"to": "bob", "amount": 5000})
        assert too_much.status_code == 500
        assert (await ingress.post("wallet/bob/history")).json() == [{"amount": 30, "note": "from alice"}]


async def test_basic_notes():
    async with serving("basic/notes.py") as ingress:
        await ingress.post("notes/alice/add", "Buy milk")
        assert (await ingress.post("notes/alice/list")).json() == ["Buy milk"]


async def test_basic_langgraph_agent():
    """The agent serves, with its companion; a message needs a model's provider, and is left to a real run."""
    pytest.importorskip("langgraph")
    async with serving("basic/langgraph_agent.py") as ingress:
        assert (await ingress.post("assistant/alice/messages", {})).json()["messages"] == []
        assert (await ingress.post("assistant/alice/status")).json() == {"answering": None, "waiting": []}


async def test_basic_create_agent(monkeypatch: pytest.MonkeyPatch):
    """create_agent's model (scripted here) calls the orders service through the Restate tool, and answers."""
    pytest.importorskip("langchain.agents")
    import langchain.agents.factory  # pylint: disable=import-outside-toplevel
    from langchain_core.language_models import BaseChatModel  # pylint: disable=import-outside-toplevel
    from langchain_core.messages import AIMessage, BaseMessage, ToolMessage  # pylint: disable=import-outside-toplevel
    from langchain_core.outputs import ChatGeneration, ChatResult  # pylint: disable=import-outside-toplevel

    class Scripted(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "scripted"

        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # pyright: ignore[reportIncompatibleMethodOverride]
            return self

        def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
            last = messages[-1]
            if isinstance(last, ToolMessage):
                reply = AIMessage(str(last.text))
            else:
                reply = AIMessage("", tool_calls=[{"id": "c1", "name": "orders_status", "args": {"input": "103"}}])
            return ChatResult(generations=[ChatGeneration(message=reply)])

    # The example names an OpenAI model; the scripted one stands in for it, with no API key.
    monkeypatch.setattr(langchain.agents.factory, "init_chat_model", lambda *args, **kwargs: Scripted())
    async with serving("basic/create_agent.py") as ingress:
        answer = await ingress.post("desk/alice/chat", "Where is my order 103?")
        assert answer.json() == "Order 103 was delivered yesterday."


async def test_basic_pydantic_ai_agent():
    """The agent serves, with its companion; a message needs a model's provider, and is left to a real run."""
    pytest.importorskip("pydantic_ai")
    async with serving("basic/pydantic_ai_agent.py") as ingress:
        assert (await ingress.post("assistant/alice/messages", {})).json()["messages"] == []
        assert (await ingress.post("assistant/alice/status")).json() == {"answering": None, "waiting": []}


async def test_basic_adk_agent(monkeypatch: pytest.MonkeyPatch):
    """The agent serves, with its companion; a message needs a model's provider, and is left to a real run."""
    pytest.importorskip("google.adk")
    monkeypatch.setenv("RESTATE_AGENT_MODEL", "google/gemini-x")  # an ADK agent needs a model
    async with serving("basic/adk_agent.py") as ingress:
        assert (await ingress.post("assistant/alice/messages", {})).json()["messages"] == []
        assert (await ingress.post("assistant/alice/status")).json() == {"answering": None, "waiting": []}


async def test_basic_openai_agent():
    """The agent serves, with its companion; a message needs a model's provider, and is left to a real run."""
    pytest.importorskip("agents")
    async with serving("basic/openai_agent.py") as ingress:
        assert (await ingress.post("assistant/alice/messages", {})).json()["messages"] == []
        assert (await ingress.post("assistant/alice/status")).json() == {"answering": None, "waiting": []}


async def test_intermediate_support():
    """The agent's tools are Restate's: the order service and the refund workflow answer, and a signal no one waits for is dropped."""
    pytest.importorskip("agents")
    async with serving("intermediate/support.py") as ingress:
        assert (await ingress.post("orders/status", {"order": "103"})).json() == "Order 103 was delivered yesterday."
        refunded = await ingress.post("refund/r1/run", {"order": "103", "amount": 30})
        assert refunded.json() == "Refunded $30 for order 103."
        assert (await ingress.post("support/alice/signal", {"name": "approval", "value": "approved"})).json() == {
            "resolved": None
        }
        assert (await ingress.post("support/alice/pending")).json() == []


async def test_intermediate_followup():
    pytest.importorskip("pydantic_ai")
    async with serving("intermediate/followup.py") as ingress:
        assert (await ingress.post("checkins/remind", {"customer": "alice", "note": "order 103"})).status_code == 200
        assert (await ingress.post("concierge/alice/messages", {})).json()["messages"] == []


async def test_intermediate_team():
    for module in ("agents", "langgraph", "pydantic_ai"):
        pytest.importorskip(module)
    async with serving("intermediate/team.py") as ingress:
        for agent in ("editor/alice", "researcher/alice.lisbon", "writer/alice.guide"):
            assert (await ingress.post(f"{agent}/messages", {})).json()["messages"] == []


async def test_intermediate_router():
    for module in ("agents", "langgraph", "pydantic_ai"):
        pytest.importorskip(module)
    async with serving("intermediate/router.py") as ingress:
        for agent in ("billing/alice", "tech/alice"):
            assert (await ingress.post(f"{agent}/messages", {})).json()["messages"] == []


async def test_intermediate_triage():
    for module in ("langgraph", "pydantic_ai"):
        pytest.importorskip(module)
    async with serving("intermediate/triage.py") as ingress:
        no_draft = await ingress.post("triage/t1/draft")
        assert no_draft.status_code == 200 and not no_draft.content  # None, before the workflow runs
        for agent in ("classifier/t1", "drafter/t1"):
            assert (await ingress.post(f"{agent}/messages", {})).json()["messages"] == []


async def test_intermediate_refund_graph():
    """Chat runs the graph: it reads the request and asks about the policy (both agents scripted here), waits for the
    approval, and pays; a rejection is answered without paying."""
    pytest.importorskip("langgraph")

    def setup(module: ModuleType) -> None:
        # In place of the example's agents, which need a model: virtual objects of the same names.
        clerk, policy = restate.VirtualObject("clerk"), restate.VirtualObject("policy")

        @clerk.handler(name="chat")
        async def read(_ctx: restate.ObjectContext, message: str) -> dict[str, Any]:
            return {"order": message.split()[1], "amount": 30}

        @policy.handler(name="chat")
        async def check(_ctx: restate.ObjectContext, message: str) -> str:
            return f"Fits the policy: {message}"

        setattr(module, "clerk", clerk)
        setattr(module, "policy", policy)

    async with serving("intermediate/refund_graph.py", setup) as ingress:
        cases = (("103", "approved", "Refunded $30 for order 103."), ("104", "no", None))
        for order, approval, receipt in cases:
            chat = asyncio.create_task(ingress.post("refunds/alice/chat", f"Order {order} arrived broken"))
            waiting: list[dict[str, Any]] = []
            for _ in range(100):
                if waiting := (await ingress.post("refunds/alice/pending")).json():
                    break
                await asyncio.sleep(0.1)
            about = f"A refund of $30 for order {order}."
            refund = {"order": order, "amount": 30}
            assert [(w["name"], w["input"]) for w in waiting] == [
                ("approval", {"refund": refund, "policy": f"Fits the policy: {about}"})
            ]
            await ingress.post("refunds/alice/signal", {"name": "approval", "value": approval})
            assert (await chat).json() == (receipt or f"The refund was not approved: {about}")


async def test_intermediate_loop(monkeypatch: pytest.MonkeyPatch):
    """The loop, on a scripted model: it looks two orders up at once, waits for approval, and refunds; a crash
    after the refund ran retries the turn, which asks the model again without refunding again; and a crash after
    the final reply was stored retries the turn, which answers with it, without asking the model again."""
    pytest.importorskip("pydantic")
    from restate.agents._conversation import Conversation  # pylint: disable=import-outside-toplevel

    asked: list[str] = []
    looked_up: list[str] = []
    refunded: list[str] = []
    crashed: list[bool] = []
    approval = "wait_for_approval"  # the name of the example's wait_for_signal("approval") tool

    def call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(arguments)}

    def reply(*calls: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"type": "reasoning", "id": f"rs-{calls[0]['call_id']}", "summary": []}, *calls]

    async def complete(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        last = items[-1]
        asked.append(last.get("call_id") or last["role"])
        if last.get("role") == "assistant":
            raise restate.TerminalError("the model was asked again after its final reply")
        if last.get("role") == "user":  # two tool calls in one reply
            return reply(call("c1", "order_status", {"order": "103"}), call("c1b", "order_status", {"order": "104"}))
        if last["call_id"] in ("c1", "c1b"):
            return reply(call("c2", approval, {"order": "103", "amount": 30}))
        if last["call_id"] == "c2":
            return reply(call("c3", "refund", {"order": "103", "amount": 30}))
        if not crashed:
            crashed.append(True)
            raise ConnectionError("the model's connection dropped")  # Restate retries the turn
        text = {"type": "output_text", "text": f"Done: {last['output']}", "annotations": []}
        return [{"type": "message", "role": "assistant", "content": [text]}]

    answered = Conversation.answered
    unanswered: list[bool] = []

    async def answered_once_crashing(conversation: Conversation, prompt_id: str, answer: str) -> None:
        if not unanswered:
            unanswered.append(True)
            raise ConnectionError("lost before the answer was stored")  # after the final reply was
        await answered(conversation, prompt_id, answer)

    monkeypatch.setattr(Conversation, "answered", answered_once_crashing)

    def setup(module: ModuleType) -> None:
        async def at_once(order: str) -> str:
            """Look an order up, once the other lookup has started too: the calls of a reply run at once."""
            looked_up.append(order)
            for _ in range(50):
                if len(looked_up) == 2:
                    return f"Order {order} was delivered yesterday."
                await asyncio.sleep(0.1)
            return f"Order {order} was looked up alone."

        def counted(order: str, amount: float) -> str:
            refunded.append(order)
            return f"Refunded ${amount:g} for order {order}."

        assert module.approval.name == approval
        setattr(module, "complete", complete)  # the model, scripted
        module.TOOLS["order_status"] = module.tool(at_once, name="order_status", description="Look an order up.")
        module.TOOLS["refund"] = module.tool(counted, name="refund", description="Refund an order.")

    async with serving("intermediate/loop.py", setup) as ingress:
        chat = asyncio.create_task(ingress.post("refunds/alice/chat", "Order 103 arrived broken"))
        waiting: list[dict[str, Any]] = []
        for _ in range(100):
            if waiting := (await ingress.post("refunds/alice/pending")).json():
                break
            await asyncio.sleep(0.1)
        assert [w["name"] for w in waiting] == ["approval"] and not refunded  # it waits, and has not refunded
        await ingress.post("refunds/alice/signal", {"name": "approval", "value": "approved"})
        assert (await chat).json() == "Done: Refunded $30 for order 103."
        assert refunded == ["103"] and crashed == [True] and unanswered == [True]
        # The model was asked once for each step (and again after the crash that cut its connection), not after the
        # crash that came once its final reply was stored.
        assert asked == ["user", asked[1], "c2", "c3", "c3"] and asked[1] in ("c1", "c1b")
        shown = (await ingress.post("refunds/alice/messages", {})).json()["messages"]
        tools = [(p["name"], p["output"]) for p in shown[1]["parts"]]
        assert tools == [
            ("order_status", "Order 103 was delivered yesterday."),
            ("order_status", "Order 104 was delivered yesterday."),
            (approval, "approved"),
            ("refund", "Refunded $30 for order 103."),
        ]
        assert shown[-1]["parts"] == [{"type": "text", "text": "Done: Refunded $30 for order 103."}]


async def test_intermediate_loop_unwinds_the_other_tool_calls_before_failing():
    """When a tool call fails, the loop cancels the others, and fails once they have unwound: before the turn closes."""
    pytest.importorskip("pydantic")
    all_of = load("intermediate/loop.py").all_of
    unwound: list[str] = []

    async def fails() -> None:
        await asyncio.sleep(0)
        raise ConnectionError("the tool failed")

    async def runs_on() -> None:
        try:
            await asyncio.sleep(10)
        finally:
            await asyncio.sleep(0.01)  # it takes a moment to unwind: say, telling the clients the call ended
            unwound.append("runs_on")

    with pytest.raises(ConnectionError, match="the tool failed"):
        await all_of([fails(), runs_on()])
    assert unwound == ["runs_on"]


def serves_http2() -> bool:
    """Whether ``restate.serve`` can run here: uvicorn 0.53+ with zttp."""
    return importlib.util.find_spec("uvicorn") is not None and importlib.util.find_spec("zttp") is not None


@pytest.mark.skipif(not serves_http2(), reason="restate.serve needs the serve extra")
async def test_an_example_as_a_program(tmp_path: pathlib.Path):
    """``uv run`` an example: it serves on PORT, Restate calls it over HTTP/2, and it stops on SIGTERM."""
    # Restate first: Docker maps its ports to free host ports, which could otherwise take the program's.
    container = RestateContainer("ghcr.io/restatedev/restate:latest", always_replay=False, disable_retries=False)
    container.start()
    port = find_free_port()
    output = tmp_path / "program.log"
    with output.open("wb") as log:
        program = subprocess.Popen(
            [sys.executable, str(EXAMPLES / "basic/notes.py")],
            env={**os.environ, "PORT": str(port)},
            stdout=log,
            stderr=log,
        )
    try:
        await listening(program, port, output)
        async with httpx2.AsyncClient(base_url=container.admin_url(), timeout=30) as admin:
            uri = f"http://host.docker.internal:{port}"
            registered = await admin.post("/deployments", json={"uri": uri, "force": True})
            assert registered.status_code < 300, registered.text
            deployment = (await admin.get(f"/deployments/{registered.json()['id']}")).json()
            assert deployment.get("http_version") == "HTTP/2.0", deployment
        ingress = Ingress(container.ingress_url())
        await ingress.post("notes/alice/add", "Buy milk")
        assert (await ingress.post("notes/alice/list")).json() == ["Buy milk"]

        # Stopped while Restate still holds its connections open: it stops within its graceful timeout.
        program.send_signal(signal.SIGTERM)
        program.wait(timeout=10)
    finally:
        program.kill()
        container.stop()


async def listening(program: subprocess.Popen[bytes], port: int, output: pathlib.Path) -> None:
    """Wait until ``program`` accepts connections on ``port``, or fail with what it printed."""
    for _ in range(200):
        assert program.poll() is None, f"the program exited:\n{output.read_text()}"
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise AssertionError(f"the program never listened on {port}:\n{output.read_text()}")
