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
import sys
import typing

from restate.connection import ConnectError, connect

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
        asyncio.run(connect(app, name=args.name, key=args.key))
    except ConnectError as error:
        print(f"error: {error}", file=sys.stderr, flush=True)
        return EXIT_FAILURE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
