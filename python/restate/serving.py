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
"""Serve a Restate app over HTTP/2: ``restate.serve``, the counterpart of ``restate.connect``."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import re
import signal
import typing

from restate.server import install_shutdown_hook
from restate.server_types import RestateAppT

GRACEFUL_SHUTDOWN_SECONDS = 3
"""How long a stopping server waits for the requests in flight; Restate retries the rest elsewhere."""

_MISSING = (
    "restate.serve runs on uvicorn's HTTP/2: install restate-sdk[serve] (uvicorn 0.53 or later, and zttp; "
    "Python 3.11 or later), or serve the app with an ASGI server of your own, such as hypercorn"
)


async def serve(app: RestateAppT, *, host: str = "0.0.0.0", port: typing.Optional[int] = None) -> None:
    """
    Serve ``app`` over HTTP/2, until SIGINT or SIGTERM stops it.

    Restate connects to a deployment with cleartext HTTP/2, which this serves (on uvicorn)::

        app = restate.app([greeter])

        if __name__ == "__main__":
            asyncio.run(restate.serve(app))

    It stops within a few seconds of SIGINT or SIGTERM: the invocations in flight are asked to wind
    down, and Restate retries those that don't finish in time.

    Args:
        app: The app to serve, as ``restate.app`` returns it.
        host: The address to listen on. Defaults to all interfaces.
        port: The port to listen on. Defaults to ``$PORT``, else 9080.

    Raises:
        ImportError: If the ``serve`` extra is not installed (``pip install restate-sdk[serve]``).
    """
    try:
        uvicorn = importlib.import_module("uvicorn")
        importlib.import_module("zttp")
    except ImportError as error:
        raise ImportError(_MISSING) from error
    version = re.match(r"(\d+)\.(\d+)", str(getattr(uvicorn, "__version__", "")))
    if version is None or (int(version[1]), int(version[2])) < (0, 53):
        raise ImportError(_MISSING)

    config = uvicorn.Config(
        app,
        host=host,
        port=port or int(os.environ.get("PORT", "9080")),
        http="zttp",
        http2=True,
        # The app has no lifespan, and a graceful shutdown would otherwise wait for Restate's open connections.
        lifespan="off",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    server = uvicorn.Server(config)

    # The app installs its own SIGTERM hook on its first request, through install_shutdown_hook, which keeps
    # one asyncio handler per signal: it would replace uvicorn's handlers. So the server's exit goes through
    # the same hook, registered first, and uvicorn captures no signals itself: both then run.
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):  # Windows, or not the main thread
            install_shutdown_hook(loop, _exit_on(server, signum), signum)
    server.capture_signals = contextlib.nullcontext
    await server.serve()


def _exit_on(server: typing.Any, signum: int) -> typing.Callable[[], None]:
    return lambda: server.handle_exit(signum, None)
