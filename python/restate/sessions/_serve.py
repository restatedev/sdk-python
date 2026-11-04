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
services they depend on (such as the ``signals`` object that agents use). Serve
it as you would ``restate.app``: with ``restate.serve``, or any ASGI server::

    asyncio.run(restate.serve(sessions.app(notes)))
"""

from __future__ import annotations

import os
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
