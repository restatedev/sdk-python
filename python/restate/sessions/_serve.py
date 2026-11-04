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
"""Serving session objects.

``app`` builds the ASGI app of a program's services, together with any
services they depend on (such as the ``signals`` object that agents use). Run
it with an ASGI server of your choice (hypercorn, uvicorn, ...), as you would
``restate.app``, or call ``serve`` to run it in one line.
"""

from __future__ import annotations

import importlib
import os
import re
import typing
from collections.abc import Callable, Iterable, Sequence

import restate
from restate.entry_codec import JournalValueCodec, JournalValueCodecProvider
from restate.server_types import RestateAppT
from restate.sessions._link import Link

Service = restate.Service | restate.VirtualObject | restate.Workflow
"""A service, virtual object (a session object too) or workflow."""

D = typing.TypeVar("D")

_NEEDS: Link[Callable[[], Sequence[Service]]] = Link("needs")
"""The services a definition needs served next to it (see ``needing``)."""


def needing(definition: D, services: Callable[[], Sequence[Service]]) -> D:
    """Declare that ``definition`` needs ``services`` served next to it, and return it.

    ``app`` then serves those services too. This is for libraries whose
    objects call a service of their own, such as the ``signals`` object of
    agents.

    Args:
        definition: The service, virtual object or workflow that depends on
            ``services``.
        services: A function returning the services needed. It is called
            lazily, when ``app`` collects the services to serve.

    Returns:
        ``definition`` itself.
    """
    return _NEEDS.on(definition, services)


def served(services: Iterable[Service], names: str | None = None) -> list[Service]:
    """Return the services that ``app`` would serve for ``services``.

    Each service is followed by the services it needs (see ``needing``),
    transitively, and each appears once.

    Args:
        services: The services to serve.
        names: A comma-separated list of service names to keep; if empty or
            ``None``, all of them are kept.

    Returns:
        The services to serve, in order.

    Raises:
        ValueError: If ``names`` contains a name that matches no service.
    """
    every: dict[str, Service] = {}

    def add(service: Service) -> None:
        if service.name in every:
            return
        every[service.name] = service
        needs = _NEEDS.of(service)
        for needed in needs() if needs is not None else ():
            add(needed)

    for service in services:
        add(service)
    wanted = [name.strip() for name in (names or "").split(",") if name.strip()]
    if not wanted:
        return list(every.values())
    unknown = [name for name in wanted if name not in every]
    if unknown:
        raise ValueError(f"RESTATE_SERVE_ONLY names {', '.join(unknown)}: the services are {', '.join(every)}")
    return [service for name, service in every.items() if name in wanted]


def app(
    *services: Service,
    protocol: typing.Literal["bidi", "request_response"] | None = None,
    identity_keys: list[str] | None = None,
    journal_value_codec: JournalValueCodec | JournalValueCodecProvider | None = None,
) -> RestateAppT:
    """Return the ASGI app that serves ``services`` and the services they need.

    This is ``restate.app`` with the dependencies declared by ``needing``
    added, and it takes the same options. Run it with any ASGI server::

        app = sessions.app(notes, orders)  # then: hypercorn module:app

    If the ``RESTATE_SERVE_ONLY`` environment variable is set to a
    comma-separated list of names (such as ``triage,planner,signals``), only
    those services are served. This lets one program, or one container image,
    be deployed in parts.

    Args:
        *services: The services, virtual objects (session objects included)
            and workflows to serve.
        protocol: Passed to ``restate.app``.
        identity_keys: Passed to ``restate.app``.
        journal_value_codec: Passed to ``restate.app``.

    Returns:
        The ASGI app.

    Raises:
        ValueError: If ``RESTATE_SERVE_ONLY`` names a service that is not served.
    """
    return restate.app(
        served(services, os.environ.get("RESTATE_SERVE_ONLY")),
        protocol=protocol,
        identity_keys=identity_keys,
        journal_value_codec=journal_value_codec,
    )


def serve(
    *services: Service,
    port: int | None = None,
    identity_keys: list[str] | None = None,
    journal_value_codec: JournalValueCodec | JournalValueCodecProvider | None = None,
) -> None:
    """Serve ``services`` over HTTP until the process is stopped.

    This runs ``app(*services)`` on uvicorn with cleartext HTTP/2, which is
    how Restate connects to services::

        if __name__ == "__main__":
            sessions.serve(wallet, handle)

    It needs the ``serve`` extra (``pip install "restate-sdk[serve]"``, which
    installs uvicorn 0.53 or later and zttp) and Python 3.11 or later. On
    other setups, serve ``app(...)`` with an ASGI server of your own. It stops
    within a few seconds of SIGINT or SIGTERM.

    Args:
        *services: The services, virtual objects (session objects included)
            and workflows to serve, as for ``app``.
        port: The port to listen on. Defaults to ``$PORT``, else 9080.
        identity_keys: Passed to ``restate.app``.
        journal_value_codec: Passed to ``restate.app``.

    Raises:
        ImportError: If uvicorn 0.53 or later, or zttp, is not installed.
    """
    try:
        uvicorn = importlib.import_module("uvicorn")
        importlib.import_module("zttp")
    except ImportError as error:
        raise ImportError(_NO_SERVER) from error
    version = re.match(r"(\d+)\.(\d+)", str(getattr(uvicorn, "__version__", "")))
    if version is None or (int(version[1]), int(version[2])) < (0, 53):
        raise ImportError(_NO_SERVER)
    config = uvicorn.Config(
        app(*services, identity_keys=identity_keys, journal_value_codec=journal_value_codec),
        host="0.0.0.0",
        port=port or int(os.environ.get("PORT", "9080")),
        http="zttp",
        http2=True,
        # The endpoint has no lifespan; and a graceful shutdown would wait for Restate's open connections.
        lifespan="off",
        timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN,
    )
    uvicorn.Server(config).run()


_NO_SERVER = (
    "sessions.serve runs on uvicorn's HTTP/2: install restate-sdk[serve] (uvicorn 0.53 or later, and zttp; "
    "Python 3.11 or later), or serve sessions.app(...) with an ASGI server of your own"
)

_GRACEFUL_SHUTDOWN = 3
"""How many seconds a stopping ``serve`` waits for requests in flight; Restate retries the rest."""
