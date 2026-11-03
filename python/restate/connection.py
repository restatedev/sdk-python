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
that connection into the regular ASGI application.

:func:`restate.connect` is the one-liner: it owns the event loop and the
signal handling and blocks until the connection stops. :class:`Connection`
is the same thing with manual control, for programs that already run an
event loop.
"""

import asyncio
import signal
import sys
import typing

from restate.server_types import RestateAppT

# pylint: disable=import-error,no-name-in-module
from restate._internal import ConnectError, TunnelEngine, TunnelEngineOptions, TunnelInfo

__all__ = ["connect", "Connection", "ConnectError", "TunnelInfo"]

# What the first signal gives in-flight invocations before the second one (or the
# deadline) cuts the connections.
DEFAULT_SHUTDOWN_GRACE_SECONDS = 120.0


class Connection:
    """
    A connection to Restate Cloud serving the application, under manual control.

    Resolving the configuration happens here, so a missing name or key fails
    at construction with :class:`ConnectError`. Nothing is dialed before
    :meth:`start`.

    Args:
        app: the ASGI application returned by :func:`restate.app`.
        name: the deployment name, shared by the replicas of one deployment. It is what
            gets registered with Restate (``connect://<name>``). Defaults to
            ``RESTATE_CONNECT_NAME``.
        key: the connect key from the Cloud UI (Development → Configure SDK). Defaults to
            ``RESTATE_CONNECT_KEY``. Under the Restate operator, its ``RESTATE_INPROC_*``
            variables are used instead.

    Example::

        connection = Connection(app, name="greeter")
        await connection.start()
        try:
            await connection.wait()
        finally:
            await connection.shutdown()
    """

    def __init__(
        self,
        app: RestateAppT,
        *,
        name: typing.Optional[str] = None,
        key: typing.Optional[str] = None,
    ) -> None:
        self._app = app
        self._options = TunnelEngineOptions.resolve(name=name, key=key)
        self._engine: typing.Optional[TunnelEngine] = None
        self._info: typing.Optional[TunnelInfo] = None

    @property
    def name(self) -> str:
        """The deployment name: register it with Restate as ``connect://<name>``."""
        return self._options.name

    @property
    def registration_identifier(self) -> str:
        """``connect://<name>``, what the CLI and the Cloud UI accept when registering."""
        return self._options.registration_identifier

    @property
    def started(self) -> bool:
        """Whether :meth:`start` completed."""
        return self._info is not None

    @property
    def info(self) -> typing.Optional[TunnelInfo]:
        """Metadata from the first successful handshake (``deployment_url``, ``proxy_url``, …), once started."""
        return self._info

    async def start(self) -> TunnelInfo:
        """
        Connect. Returns after the first successful handshake, when the application
        can receive invocations. Must run inside the event loop that will serve the
        application.

        Raises:
            ConnectError: Restate Cloud rejected the connection, or it could not be
                established at all.
        """
        if self._engine is not None:
            raise RuntimeError("Connection.start() was already called")
        engine = TunnelEngine(self._app, self._options)
        self._engine = engine
        try:
            self._info = await engine.ready()
        except BaseException:
            await engine.shutdown(0.0)
            raise
        return self._info

    async def wait(self) -> None:
        """
        Wait until the connection stops serving: returns after :meth:`shutdown`, raises
        :class:`ConnectError` when the connection failed for good (rejected credentials,
        an exhausted reconnect policy, …).
        """
        if self._engine is None:
            raise RuntimeError("Connection.start() was not called")
        await self._engine.terminated()

    async def shutdown(self, grace: float = DEFAULT_SHUTDOWN_GRACE_SECONDS) -> None:
        """
        Stop serving. New invocations are refused and Restate Cloud is told; in-flight
        invocations get ``grace`` seconds to finish before the connections are cut.
        ``0`` cuts them at once. A no-op before :meth:`start`, and never raises: a
        connection that already failed reports its error through :meth:`start` or
        :meth:`wait`, not here.
        """
        if self._engine is None:
            return
        try:
            await self._engine.shutdown(grace)
        except ConnectError:
            pass


def connect(
    app: RestateAppT,
    *,
    name: typing.Optional[str] = None,
    key: typing.Optional[str] = None,
) -> None:
    """
    Serve ``app`` through an outbound connection to Restate Cloud, until stopped.

    Owns the event loop and the signal handling: the first SIGINT or SIGTERM drains
    in-flight invocations for up to two minutes, a second one stops at once. Returns
    when the connection has stopped.

    Args:
        app: the ASGI application returned by :func:`restate.app`.
        name: the deployment name, registered with Restate as ``connect://<name>``.
            Defaults to ``RESTATE_CONNECT_NAME``.
        key: the connect key from the Cloud UI (Development → Configure SDK). Defaults to
            ``RESTATE_CONNECT_KEY``.

    Raises:
        ConnectError: the configuration is incomplete or invalid, or Restate Cloud
            rejected the connection.

    Example::

        app = restate.app(services=[greeter])
        restate.connect(app, name="greeter")
    """
    asyncio.run(_serve(Connection(app, name=name, key=key)))


async def _serve(connection: Connection) -> None:
    # pylint: disable=import-outside-toplevel
    from restate.server import install_shutdown_hook

    loop = asyncio.get_running_loop()
    await connection.start()
    signals_seen = 0

    def on_signal() -> None:
        nonlocal signals_seen
        signals_seen += 1
        if signals_seen == 1:
            print(
                f"Shutting down: waiting up to {DEFAULT_SHUTDOWN_GRACE_SECONDS:.0f}s for in-flight "
                "invocations (send the signal again to stop now)",
                file=sys.stderr,
                flush=True,
            )
            loop.create_task(connection.shutdown(DEFAULT_SHUTDOWN_GRACE_SECONDS))
        elif signals_seen == 2:
            print("Stopping now", file=sys.stderr, flush=True)
            loop.create_task(connection.shutdown(0.0))

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            install_shutdown_hook(loop, on_signal, signum)
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # Windows or non-main thread: no signal handling, stop through shutdown()

    print(
        f"Connected to Restate Cloud as {connection.name}. Register this deployment as "
        f"{connection.registration_identifier}",
        file=sys.stderr,
        flush=True,
    )
    try:
        await connection.wait()
    finally:
        await connection.shutdown(0.0)
