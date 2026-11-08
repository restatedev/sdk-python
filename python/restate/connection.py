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

:func:`restate.connect` is the one-liner (``asyncio.run(restate.connect(app))``):
it handles the signals and runs until the connection stops.
:func:`restate.with_connection` is the context manager for programs that want
to do something while connected.
"""

import asyncio
import logging
import signal
import typing
import warnings
from contextlib import asynccontextmanager

from restate.server_types import RestateAppT

# pylint: disable=import-error,no-name-in-module
from restate._internal import ConnectError, TunnelEngine, TunnelEngineOptions, TunnelInfo

__all__ = ["connect", "with_connection", "Connection", "ConnectError", "TunnelInfo"]

logger = logging.getLogger(__name__)

# What the first signal gives in-flight invocations before the second one (or the
# deadline) cuts the connections.
SIGNAL_SHUTDOWN_GRACE_SECONDS = 120.0

# How long leaving a ``with_connection()`` block waits for in-flight invocations.
EXIT_SHUTDOWN_GRACE_SECONDS = 10.0


class Connection:
    """
    The live connection :func:`with_connection` yields. Not meant to be constructed.
    """

    def __init__(self, engine: TunnelEngine, options: TunnelEngineOptions, info: TunnelInfo) -> None:
        self._engine = engine
        self._options = options
        self._info = info

    @property
    def name(self) -> str:
        """The deployment name: register it with Restate as ``connect://<name>``."""
        return self._options.name

    @property
    def registration_identifier(self) -> str:
        """``connect://<name>``, what the CLI and the Cloud UI accept when registering."""
        return self._options.registration_identifier

    @property
    def info(self) -> TunnelInfo:
        """Metadata from the first successful handshake (``deployment_url``, ``proxy_url``, …)."""
        return self._info

    async def wait_closed(self) -> None:
        """
        Wait until the connection stops serving: returns after :meth:`shutdown`, raises
        :class:`ConnectError` when the connection failed for good (rejected credentials,
        an exhausted reconnect policy, …).
        """
        await self._engine.terminated()

    async def wait(self) -> None:
        """
        Deprecated alias of :meth:`wait_closed`.

        .. deprecated:: 1.4.0
            Use :meth:`wait_closed` instead.
        """
        warnings.warn(
            "Connection.wait() is deprecated, use Connection.wait_closed() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        await self.wait_closed()

    async def shutdown(self, grace: float = 0.0) -> None:
        """
        Stop serving. New invocations are refused and Restate Cloud is told; in-flight
        invocations get ``grace`` seconds to finish before the connections are cut.
        ``0`` cuts them at once. Never raises: a connection that already failed reports
        its error through :meth:`wait_closed`, not here.
        """
        try:
            await self._engine.shutdown(grace)
        except ConnectError:
            pass


@asynccontextmanager
async def with_connection(
    app: RestateAppT,
    *,
    name: typing.Optional[str] = None,
    key: typing.Optional[str] = None,
) -> typing.AsyncIterator[Connection]:
    """
    Serve ``app`` through an outbound connection to Restate Cloud while inside the block.

    Entering waits for the first successful handshake. Leaving drains in-flight
    invocations for up to ten seconds, then cuts the connections; call
    :meth:`Connection.shutdown` first to leave faster. No signal handling: that is the
    caller's business here (see :func:`connect` for the batteries-included form).

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

        async with restate.with_connection(app, name="greeter") as connection:
            print(connection.info.deployment_url)
            await connection.wait_closed()
    """
    options = TunnelEngineOptions.resolve(name=name, key=key)
    engine = TunnelEngine(app, options)
    logger.info("Connecting to Restate Cloud environment %s (%s)", options.environment_id, options.region)
    try:
        info = await engine.ready()
    except BaseException:
        await engine.shutdown(0.0)
        raise
    logger.debug("Deployment URL: %s", info.deployment_url)
    logger.info('Connected as "%s"', options.name)
    logger.info("Register it once:")
    logger.info("  restate deployments register %s", options.registration_identifier)
    logger.info("  or open %s", options.registration_deep_link)
    connection = Connection(engine, options, info)
    try:
        yield connection
    finally:
        await connection.shutdown(EXIT_SHUTDOWN_GRACE_SECONDS)


async def connect(
    app: RestateAppT,
    *,
    name: typing.Optional[str] = None,
    key: typing.Optional[str] = None,
) -> None:
    """
    Serve ``app`` through an outbound connection to Restate Cloud, until stopped.

    The first SIGINT or SIGTERM drains in-flight invocations for up to two minutes, a
    second one stops at once. Returns when the connection has stopped.

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

        if __name__ == "__main__":
            asyncio.run(restate.connect(app, name="greeter"))
    """
    # pylint: disable=import-outside-toplevel
    from restate.server import install_shutdown_hook

    _default_logging()
    loop = asyncio.get_running_loop()
    async with with_connection(app, name=name, key=key) as connection:
        signals_seen = 0

        def on_signal() -> None:
            nonlocal signals_seen
            signals_seen += 1
            if signals_seen == 1:
                logger.info(
                    "Shutting down: waiting up to %.0fs for in-flight invocations (send the signal again to stop now)",
                    SIGNAL_SHUTDOWN_GRACE_SECONDS,
                )
                loop.create_task(connection.shutdown(SIGNAL_SHUTDOWN_GRACE_SECONDS))
            elif signals_seen == 2:
                logger.info("Stopping now")
                loop.create_task(connection.shutdown(0.0))

        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                install_shutdown_hook(loop, on_signal, signum)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # Windows or non-main thread: no signal handling, stop through shutdown()

        await connection.wait_closed()


def _default_logging() -> None:
    # Only when the application configured no logging of its own.
    restate_logger = logging.getLogger("restate")
    if logging.getLogger().handlers or restate_logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[restate] %(levelname)s: %(message)s"))
    restate_logger.addHandler(handler)
    if restate_logger.level == logging.NOTSET:
        restate_logger.setLevel(logging.INFO)
