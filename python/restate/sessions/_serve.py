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
``app``: the ASGI app of a program's services, with what they need beside them
(a library's objects, such as an agent's ``signals``). As sdk-sessions'
``serve.ts``; serving it is the program's (hypercorn, uvicorn...), as for
``restate.app``, or ``serve``'s.
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
"""The services a definition needs served beside it: see ``needing``."""


def needing(definition: D, services: Callable[[], Sequence[Service]]) -> D:
    """
    ``definition``, which needs ``services`` served beside it: ``app`` serves
    them too. For a library whose objects call one of their own (an agent's
    ``signals``, say). ``services`` is called once ``app`` asks.
    """
    return _NEEDS.on(definition, services)


def served(services: Iterable[Service], names: str | None = None) -> list[Service]:
    """
    What ``app`` serves of ``services``: each with what it needs beside it,
    once, of those ``names`` names (comma separated; all of them without). A
    name of none is an error.
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
    """
    The ASGI app of ``services``, with the services they need beside them,
    each once: ``restate.app``'s, whose options it takes. ``RESTATE_SERVE_ONLY``
    (names separated by commas, such as ``triage,planner,signals``) serves only
    those of them: one program (or image), deployed in parts::

        app = sessions.app(notes, orders)   # hypercorn module:app
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
    """
    Serve ``services`` (``app``'s: with what they need beside them, of those
    ``RESTATE_SERVE_ONLY`` names) on ``port``, or PORT, or 9080, until the
    process stops: a program of session objects and agents in one line::

        if __name__ == "__main__":
            sessions.serve(wallet, handle)

    It runs them on uvicorn's HTTP/2 (cleartext, as Restate calls them),
    imported here: install the ``serve`` extra (uvicorn 0.53 or later, and
    zttp; Python 3.11 or later), or serve ``app``'s ASGI app with a server of
    your own. It stops on SIGINT or SIGTERM, within a few seconds.
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
    "sessions.serve runs on uvicorn's HTTP/2: install restate_sdk[serve] (uvicorn 0.53 or later, and zttp; "
    "Python 3.11 or later), or serve sessions.app(...) with an ASGI server of your own"
)

_GRACEFUL_SHUTDOWN = 3
"""Seconds a stopping ``serve`` waits for the requests in flight, which Restate retries elsewhere."""
