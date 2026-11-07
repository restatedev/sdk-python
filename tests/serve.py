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
"""restate.serve and `python -m restate serve`: a program serves its app over HTTP/2, and stops on SIGTERM."""

# pylint: disable=missing-function-docstring

import asyncio
import importlib.util
import os
import pathlib
import signal
import subprocess
import sys
import time

import httpx2
import pytest

from restate.harness import RestateContainer, find_free_port
from support import RESTATE_IMAGE  # pylint: disable=import-error


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


WITHOUT_UVICORN = """
import asyncio, sys
sys.modules["uvicorn"] = None
import restate
try:
    asyncio.run(restate.serve(restate.app([])))
except ImportError as error:
    print(error)
"""


def test_without_the_serve_extra_it_says_what_to_install():
    ran = subprocess.run([sys.executable, "-c", WITHOUT_UVICORN], capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stderr
    assert "install restate-sdk[serve]" in ran.stdout


CLI_WITHOUT_UVICORN = """
import sys
sys.modules["uvicorn"] = None
from restate.__main__ import main
sys.exit(main(["serve", "restate:app"]))
"""


def test_the_cli_without_the_serve_extra_says_what_to_install():
    ran = subprocess.run([sys.executable, "-c", CLI_WITHOUT_UVICORN], capture_output=True, text=True, check=False)
    assert ran.returncode == 1, ran.stderr
    assert "install restate-sdk[serve]" in ran.stderr


APP = """
import restate

greeter = restate.Service("greeter")

@greeter.handler()
async def greet(ctx: restate.Context, name: str) -> str:
    return f"Hello {name}!"

app = restate.app([greeter])
"""

PROGRAM = (
    APP
    + """
import asyncio

asyncio.run(restate.serve(app))
"""
)


def serves_http2() -> bool:
    return importlib.util.find_spec("uvicorn") is not None and importlib.util.find_spec("zttp") is not None


@pytest.mark.anyio
@pytest.mark.server
@pytest.mark.skipif(not serves_http2(), reason="needs the serve extra")
@pytest.mark.parametrize("command", ["program", "cli"])
async def test_a_program_serves_its_app_over_http2_and_stops_on_sigterm(tmp_path: pathlib.Path, command: str):
    # Restate first: Docker maps its ports to free host ports, which could otherwise take the program's.
    container = RestateContainer(RESTATE_IMAGE, always_replay=False, disable_retries=False)
    container.start()
    port = find_free_port()
    output = tmp_path / "program.log"
    with output.open("wb") as log:
        if command == "program":
            argv = [sys.executable, "-c", PROGRAM]
        else:  # python -m restate serve, which imports the app from the working directory
            (tmp_path / "greeter_app.py").write_text(APP)
            argv = [sys.executable, "-m", "restate", "serve", "greeter_app:app", "--port", str(port)]
        env = {**os.environ, "PORT": str(port)} if command == "program" else os.environ
        program = subprocess.Popen(argv, cwd=tmp_path, env=env, stdout=log, stderr=log)
    try:
        await listening(program, port, output)
        async with httpx2.AsyncClient(base_url=container.admin_url(), timeout=30) as admin:
            uri = f"http://host.docker.internal:{port}"
            registered = await admin.post("/deployments", json={"uri": uri, "force": True})
            assert registered.status_code < 300, registered.text
            deployment = (await admin.get(f"/deployments/{registered.json()['id']}")).json()
            assert deployment.get("http_version") == "HTTP/2.0", deployment
        async with httpx2.AsyncClient(timeout=30) as ingress:
            answer = await ingress.post(f"{container.ingress_url()}/greeter/greet", json="Alice")
        assert answer.json() == "Hello Alice!"

        # Stopped while Restate still holds its connections open: it stops within its graceful timeout.
        program.send_signal(signal.SIGTERM)
        started = time.monotonic()
        program.wait(timeout=10)
        assert time.monotonic() - started < 10, output.read_text()
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
