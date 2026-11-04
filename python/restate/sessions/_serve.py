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
``restate.app``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import signal
import typing
from collections.abc import Callable, Coroutine, Iterable, Sequence
from typing import Any

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

    It runs them on hypercorn, imported here: install it, or serve ``app``'s
    ASGI app with a server of your own.
    """
    try:
        hypercorn_asyncio = importlib.import_module("hypercorn.asyncio")
        hypercorn_config = importlib.import_module("hypercorn.config")
    except ImportError as error:
        raise ImportError(
            "sessions.serve runs on hypercorn: install it, or serve sessions.app(...) with an ASGI server of your own"
        ) from error
    config: Any = hypercorn_config.Config()
    config.bind = [f"0.0.0.0:{port or int(os.environ.get('PORT', '9080'))}"]
    # Restate keeps its connections open and streams many invocations over each.
    config.h2_max_concurrent_streams = 2147483647
    config.keep_alive_max_requests = 2147483647
    config.keep_alive_timeout = 2147483647
    served_app = app(*services, identity_keys=identity_keys, journal_value_codec=journal_value_codec)
    run = typing.cast(Callable[..., Coroutine[Any, Any, None]], hypercorn_asyncio.serve)
    asyncio.run(_until_stopped(run, served_app, config))


async def _until_stopped(run: Callable[..., Coroutine[Any, Any, None]], served_app: Any, config: Any) -> None:
    """
    Serve until SIGINT or SIGTERM, then shut down: gracefully, but not for
    longer than hypercorn's graceful timeout and a little more. A graceful
    shutdown waits for every open connection to close, and Restate keeps its
    own open.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for name in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(getattr(signal, name), stop.set)
    serving = asyncio.ensure_future(run(served_app, config, shutdown_trigger=stop.wait))
    stopping = asyncio.ensure_future(stop.wait())
    await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
    stopping.cancel()
    if serving.done():
        serving.result()
        return
    try:
        await asyncio.wait_for(asyncio.shield(serving), timeout=float(config.graceful_timeout) + 2)
    except asyncio.TimeoutError:
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving
