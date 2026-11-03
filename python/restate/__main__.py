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
"""
``python -m restate``: command-line entry points.

    python -m restate connect app:app --name greeter

serves the ASGI application ``app`` of module ``app`` through an outbound
connection to Restate Cloud, until SIGINT or SIGTERM.
"""

import argparse
import asyncio
import importlib
import os
import signal
import sys
import typing

from restate.connect import ConnectError, connect
from restate.server import install_shutdown_hook

# What the first signal gives in-flight invocations before the second one (or the
# deadline) cuts the connections.
SIGNAL_SHUTDOWN_GRACE_SECONDS = 120.0

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


def _load_app(spec: str) -> typing.Any:
    """Import ``module[:attribute]`` (attribute defaults to ``app``), like hypercorn."""
    module_name, _, attribute = spec.partition(":")
    if not module_name:
        raise ValueError(f"invalid application {spec!r}: expected module:attribute")
    attribute = attribute or "app"
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise ValueError(f"cannot import module {module_name!r}: {error}") from error
    try:
        return getattr(module, attribute)
    except AttributeError as error:
        raise ValueError(f"module {module_name!r} has no attribute {attribute!r}") from error


async def _serve(app: typing.Any, name: typing.Optional[str], key: typing.Optional[str]) -> int:
    loop = asyncio.get_running_loop()
    async with connect(app, name=name, key=key) as connection:
        stopped = loop.create_future()
        signals_seen = 0

        def on_signal() -> None:
            nonlocal signals_seen
            signals_seen += 1
            if signals_seen == 1:
                print(
                    f"Shutting down: waiting up to {SIGNAL_SHUTDOWN_GRACE_SECONDS:.0f}s for in-flight "
                    "invocations (send the signal again to stop now)",
                    file=sys.stderr,
                    flush=True,
                )
                loop.create_task(connection.shutdown(SIGNAL_SHUTDOWN_GRACE_SECONDS))
            elif signals_seen == 2:
                print("Stopping now", file=sys.stderr, flush=True)
                loop.create_task(connection.shutdown(0.0))

        # The application installs its own SIGTERM hook (it unblocks invocations waiting
        # for Restate to close their input); both run through the same dispatcher.
        install_shutdown_hook(loop, on_signal, signal.SIGTERM)
        install_shutdown_hook(loop, on_signal, signal.SIGINT)

        print(
            f"Connected to Restate Cloud as {connection.name}. Register this deployment as "
            f"{connection.registration_identifier}",
            file=sys.stderr,
            flush=True,
        )
        try:
            await connection.wait()
        finally:
            stopped.cancel()
        return EXIT_OK


def main(argv: typing.Optional[typing.Sequence[str]] = None) -> int:
    """Entry point of ``python -m restate``."""
    parser = argparse.ArgumentParser(prog="python -m restate", description="Restate SDK command line.")
    commands = parser.add_subparsers(dest="command", required=True)
    connect_parser = commands.add_parser(
        "connect",
        help="serve an application through an outbound connection to Restate Cloud",
        description=(
            "Serve the ASGI application through an outbound connection to Restate Cloud, so it needs "
            "no inbound port. The connect key comes from the Cloud UI (Development → Configure SDK)."
        ),
    )
    connect_parser.add_argument(
        "app",
        help="the application as module:attribute, for example app:app (attribute defaults to app)",
    )
    connect_parser.add_argument(
        "--name",
        help="the deployment name, registered as connect://<name> (default: $RESTATE_CONNECT_NAME)",
    )
    connect_parser.add_argument(
        "--key",
        help="the connect key (default: $RESTATE_CONNECT_KEY; prefer the variable over the flag)",
    )
    args = parser.parse_args(argv)

    # Resolve the application from the working directory, like a server would.
    sys.path.insert(0, os.getcwd())
    try:
        app = _load_app(args.app)
    except ValueError as error:
        parser.error(str(error))
        return EXIT_USAGE  # pragma: no cover - parser.error exits

    try:
        return asyncio.run(_serve(app, args.name, args.key))
    except ConnectError as error:
        print(f"error: {error}", file=sys.stderr, flush=True)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
