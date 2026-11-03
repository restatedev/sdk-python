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
Connect a Restate application to Restate Cloud without exposing a port.

The application dials Restate Cloud outbound; invocations are forwarded over
that connection into the regular ASGI application. See :func:`connect`.
"""

import sys
import typing
from contextlib import asynccontextmanager

from restate.server_types import RestateAppT

# pylint: disable=import-error,no-name-in-module
from restate._internal import ConnectError, TunnelEngine, TunnelEngineOptions, TunnelInfo

__all__ = ["connect", "Connection", "ConnectError", "TunnelInfo"]

# How long leaving the ``async with restate.connect(...)`` block waits for in-flight
# invocations before cutting the connections.
EXIT_SHUTDOWN_GRACE_SECONDS = 10.0


class Connection:
    """A live connection to Restate Cloud, serving the application."""

    def __init__(self, engine: TunnelEngine, options: TunnelEngineOptions, info: TunnelInfo) -> None:
        self._engine = engine
        self._options = options
        self._info = info

    @property
    def name(self) -> str:
        """The deployment name: register it with Restate as ``connect://<name>``."""
        return self._options.name

    @property
    def info(self) -> TunnelInfo:
        """Metadata from the first successful handshake (``deployment_url``, ``proxy_url``, …)."""
        return self._info

    @property
    def registration_identifier(self) -> str:
        """``connect://<name>``, what the CLI and the Cloud UI accept when registering."""
        return self._options.registration_identifier

    async def wait(self) -> None:
        """
        Wait until the connection stops serving: returns after :meth:`shutdown`, raises
        :class:`ConnectError` when the connection failed for good (rejected credentials,
        an exhausted reconnect policy, …).
        """
        await self._engine.terminated()

    async def shutdown(self, grace: float = 0.0) -> None:
        """
        Stop serving. New invocations are refused and Restate Cloud is told; in-flight
        invocations get ``grace`` seconds to finish before the connections are cut.
        ``0`` cuts them at once.
        """
        await self._engine.shutdown(grace)


@asynccontextmanager
async def connect(
    app: RestateAppT,
    *,
    name: typing.Optional[str] = None,
    key: typing.Optional[str] = None,
) -> typing.AsyncIterator[Connection]:
    """
    Serve ``app`` through an outbound connection to Restate Cloud.

    Entering the block waits for the first successful handshake; the application then
    receives invocations until the block is left, which drains in-flight invocations for
    up to ten seconds. Call :meth:`Connection.shutdown` first to leave faster, or
    :meth:`Connection.wait` to block until the connection stops.

    Args:
        app: the ASGI application returned by :func:`restate.app`.
        name: the deployment name, shared by the replicas of one deployment. It is what
            gets registered with Restate (``connect://<name>``). Defaults to
            ``RESTATE_CONNECT_NAME``.
        key: the connect key from the Cloud UI (Development → Configure SDK). Defaults to
            ``RESTATE_CONNECT_KEY``. Under the Restate operator, its ``RESTATE_INPROC_*``
            variables are used instead.

    Raises:
        ConnectError: the configuration is incomplete or invalid, or Restate Cloud
            rejected the connection.

    Example::

        async with restate.connect(app, name="greeter") as connection:
            await connection.wait()
    """
    options = TunnelEngineOptions.resolve(name=name, key=key)
    engine = TunnelEngine(app, options)
    try:
        info = await engine.ready()
    except BaseException:
        await engine.shutdown(0.0)
        raise
    connection = Connection(engine, options, info)
    try:
        yield connection
    finally:
        try:
            await connection.shutdown(EXIT_SHUTDOWN_GRACE_SECONDS)
        except ConnectError:
            # The connection already failed and the failure was (or is being) reported
            # through wait(); do not replace that exception with the same one.
            if sys.exc_info()[0] is None:
                raise
