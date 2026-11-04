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
import os
import pathlib
import signal
import subprocess
import sys
import typing
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from types import ModuleType

import httpx
import pytest

import restate
from restate import sessions
from restate.harness import RestateContainer, find_free_port


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = [pytest.mark.anyio]

EXAMPLES = pathlib.Path(__file__).parent.parent / "examples-agents"


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

    async def post(self, path: str, body: object = None) -> httpx.Response:
        async with httpx.AsyncClient(timeout=60) as client:
            return await client.post(f"{self.url}/{path}", json=body)


@asynccontextmanager
async def serving(path: str) -> AsyncGenerator[Ingress, None]:
    """The session objects of the example at ``path``, served to a Restate server of their own."""
    module = load(path)
    objects = [value for value in vars(module).values() if isinstance(value, sessions.SessionObject)]
    async with restate.create_test_harness(
        sessions.app(*objects), restate_image="ghcr.io/restatedev/restate:latest"
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


async def test_basic_pydantic_ai_agent():
    """The agent serves, with its companion; a message needs a model's provider, and is left to a real run."""
    pytest.importorskip("pydantic_ai")
    async with serving("basic/pydantic_ai_agent.py") as ingress:
        assert (await ingress.post("assistant/alice/messages", {})).json()["messages"] == []
        assert (await ingress.post("assistant/alice/status")).json() == {"answering": None, "waiting": []}


def serves_http2() -> bool:
    """Whether ``sessions.serve`` can run here: uvicorn 0.53+ with zttp."""
    return importlib.util.find_spec("uvicorn") is not None and importlib.util.find_spec("zttp") is not None


@pytest.mark.skipif(not serves_http2(), reason="sessions.serve needs the serve extra (Python 3.11+)")
async def test_an_example_as_a_program():
    """``uv run`` an example: it serves on PORT, Restate calls it over HTTP/2, and it stops on SIGTERM."""
    port = find_free_port()
    program = subprocess.Popen(
        [sys.executable, str(EXAMPLES / "basic/notes.py")],
        env={**os.environ, "PORT": str(port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    container = RestateContainer("ghcr.io/restatedev/restate:latest", always_replay=False, disable_retries=False)
    try:
        container.start()
        async with httpx.AsyncClient(base_url=container.admin_url(), timeout=30) as admin:
            uri = f"http://host.docker.internal:{port}"
            registered = await admin.post("/deployments", json={"uri": uri, "force": True})
            for _ in range(100):  # until the program serves
                if registered.status_code < 300:
                    break
                await asyncio.sleep(0.1)
                registered = await admin.post("/deployments", json={"uri": uri, "force": True})
            assert registered.status_code < 300, registered.text
            deployment = (await admin.get(f"/deployments/{registered.json()['id']}")).json()
            assert deployment.get("http_version") == "HTTP/2.0", deployment
        ingress = Ingress(container.ingress_url())
        await ingress.post("notes/alice/add", "Buy milk")
        assert (await ingress.post("notes/alice/list")).json() == ["Buy milk"]
    finally:
        container.stop()
        program.send_signal(signal.SIGTERM)
        try:
            program.wait(timeout=10)
        finally:
            program.kill()
