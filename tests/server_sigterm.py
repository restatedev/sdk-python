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
"""The app's SIGTERM hook leaves the ASGI server's own SIGTERM handling in place, so the server still stops."""

# pylint: disable=missing-function-docstring

import asyncio
import importlib.util
import os
import signal
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable

import pytest

from restate.harness import find_free_port
from restate.server import install_shutdown_hook

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


def raise_sigterm_and_wait(loop: asyncio.AbstractEventLoop, heard: list[str]) -> list[str]:
    """Send this process SIGTERM, and run the loop until two handlers have heard it."""

    async def wait_for_both() -> list[str]:
        os.kill(os.getpid(), signal.SIGTERM)
        for _ in range(100):
            if len(heard) == 2:
                break
            await asyncio.sleep(0.01)
        return sorted(heard)

    return loop.run_until_complete(wait_for_both())


@pytest.fixture
def loop():
    """A fresh event loop, with this process's SIGTERM handling restored afterwards."""
    previous = signal.getsignal(signal.SIGTERM)
    loop = asyncio.new_event_loop()
    yield loop
    loop.remove_signal_handler(signal.SIGTERM)
    loop.close()
    signal.signal(signal.SIGTERM, previous)


def test_a_server_handler_installed_the_asyncio_way_still_runs(loop: asyncio.AbstractEventLoop):
    heard: list[str] = []
    loop.add_signal_handler(signal.SIGTERM, heard.append, "server")  # as hypercorn does
    install_shutdown_hook(loop, lambda: heard.append("app"), signal.SIGTERM, chain=True)  # as the app does
    assert raise_sigterm_and_wait(loop, heard) == ["app", "server"]


def test_a_server_handler_installed_with_signal_signal_still_runs(loop: asyncio.AbstractEventLoop):
    heard: list[str] = []
    signal.signal(signal.SIGTERM, lambda signum, frame: heard.append("server"))  # as uvicorn does
    install_shutdown_hook(loop, lambda: heard.append("app"), signal.SIGTERM, chain=True)  # as the app does
    assert raise_sigterm_and_wait(loop, heard) == ["app", "server"]


# --- Real servers, each in a process of its own ---------------------------------------------------

APP = """
import restate

greeter = restate.Service("greeter")

@greeter.handler()
async def greet(ctx: restate.Context, name: str) -> str:
    return f"Hello {name}!"

app = restate.app([greeter])
"""

HYPERCORN_FROM_CODE = """
import asyncio, hypercorn, hypercorn.asyncio
from app import app

config = hypercorn.Config()
config.bind = ["127.0.0.1:{port}"]
asyncio.run(hypercorn.asyncio.serve(app, config))
"""


def stops_on_sigterm_after_a_request(command: Callable[[int], list[str]], tmp_path) -> None:
    """Start the server, make a request (the app installs its SIGTERM hook on the first), send SIGTERM."""
    (tmp_path / "app.py").write_text(APP)
    port = find_free_port()
    server = subprocess.Popen(command(port), cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).read()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("the server never answered")
        server.send_signal(signal.SIGTERM)
        server.wait(timeout=10)
    finally:
        server.kill()


@pytest.mark.skipif(importlib.util.find_spec("uvicorn") is None, reason="needs uvicorn")
def test_uvicorn_stops_on_sigterm_after_serving(tmp_path):
    stops_on_sigterm_after_a_request(
        lambda port: [sys.executable, "-m", "uvicorn", "app:app", "--port", str(port)], tmp_path
    )


@pytest.mark.skipif(importlib.util.find_spec("hypercorn") is None, reason="needs hypercorn")
def test_hypercorn_started_from_code_stops_on_sigterm_after_serving(tmp_path):
    stops_on_sigterm_after_a_request(
        lambda port: [sys.executable, "-c", HYPERCORN_FROM_CODE.format(port=port)], tmp_path
    )
