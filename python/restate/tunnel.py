#
#  Copyright (c) 2023-2026 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-python/blob/main/LICENSE
#
# pylint: disable=line-too-long
"""
Embedded relay-tunnel receiver.

Serve a Restate deployment as a relay *receiver* using the embedded native
engine (``restate-sdk-shared-core``, ``tunnel`` feature) — the same Rust engine
the Java (FFM) and TypeScript (napi) SDKs drive. The engine dials the relay on
its own tokio runtime and bridges each forwarded request over a **loopback
socket** into a local HTTP/2 (h2c) server that hosts this SDK's ASGI app. The
native boundary carries only control (start / status / stop); no per-request
data crosses it, so request dispatch — and therefore both the promise API and
the codegen SDK — is untouched.

Requires the ``tunnel`` extra (``pip install restate-sdk[tunnel]``) for
Hypercorn.

    from restate.tunnel import serve_tunnel, RelayOptions

    handle = serve_tunnel(
        services=[greeter],
        relay=RelayOptions(
            address="relay.example:8080",
            env="myenv",
            tunnel="mytunnel",
            api_key=os.environ["RELAY_API_KEY"],
        ),
    )
    # handle.status() -> TunnelStatus(running=..., last_error=...)
    # handle.stop()    on shutdown

.. note::
   This is the loopback-over-Hypercorn implementation. A follow-up will let the
   native engine drive the ASGI app directly (no Hypercorn, no socket) — see
   ``development/relay-receiver-plan.md``.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

from restate._internal import RelayTunnel as _RelayTunnel  # type: ignore
from restate.endpoint import app as _restate_app

# Huge H2 limits: Restate holds many long-lived bidi streams (mirrors
# examples/hypercorn-config.toml and harness.py).
_H2_MAX_CONCURRENT_STREAMS = 2147483647
_KEEP_ALIVE_MAX_REQUESTS = 2147483647
_KEEP_ALIVE_TIMEOUT = 2147483647


@dataclass
class RelayOptions:
    """Where and how to register with the relay."""

    address: str
    """``host:port`` of the relay's receiver port (or the LB in front of it)."""
    env: str
    """The env (routing namespace) to register under."""
    tunnel: str
    """The tunnel to register under (scoped by ``env``)."""
    api_key: str
    """The API key presented in the ``/whoami`` handshake."""
    connections: Optional[int] = None
    """R4 multi-homing: connection slots to fan out. ``None`` = one per resolved node."""
    instance_id: Optional[str] = None
    """R5 receiver-instance id (affinity). ``None`` = auto-generate an ephemeral id."""


@dataclass
class TunnelStatus:
    """A snapshot of the tunnel's runtime status."""

    running: bool
    last_error: Optional[str]


class TunnelHandle:
    """A running tunnel plus its private loopback Hypercorn server."""

    def __init__(
        self,
        native: Any,
        local_port: int,
        stop_event: threading.Event,
        thread: threading.Thread,
    ) -> None:
        self._native = native
        self._local_port = local_port
        self._stop_event = stop_event
        self._thread = thread

    @property
    def local_port(self) -> int:
        """The loopback port the local Hypercorn server is bound to (127.0.0.1)."""
        return self._local_port

    def status(self) -> TunnelStatus:
        """Current engine status."""
        raw = json.loads(self._native.status())
        return TunnelStatus(
            running=bool(raw.get("running", False)),
            last_error=raw.get("last_error"),
        )

    def stop(self) -> None:
        """Stop the tunnel (graceful) and shut down the loopback server. Idempotent."""
        self._native.stop()
        self._stop_event.set()
        self._thread.join(timeout=5.0)

    def __enter__(self) -> "TunnelHandle":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop()


def _free_loopback_port() -> int:
    """Pick an ephemeral loopback port (mirrors harness.find_free_port)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_listening(port: int, timeout: float = 15.0) -> bool:
    """Block until the loopback server accepts a connection, so the tunnel never
    forwards a request before the ASGI server is up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


async def _shutdown_trigger(stop_event: threading.Event) -> None:
    """Resolve when ``stop_event`` is set — thread-safe across the Hypercorn loop
    (a plain ``asyncio.Event`` would be bound to the wrong loop)."""
    await asyncio.get_running_loop().run_in_executor(None, stop_event.wait)


def _run_hypercorn(asgi_app: Any, port: int, stop_event: threading.Event) -> None:
    """Serve ``asgi_app`` on 127.0.0.1:``port`` (h2c) until ``stop_event`` is set."""
    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    async def _serve() -> None:
        config = Config()
        config.bind = [f"127.0.0.1:{port}"]
        config.h2_max_concurrent_streams = _H2_MAX_CONCURRENT_STREAMS
        config.keep_alive_max_requests = _KEEP_ALIVE_MAX_REQUESTS
        config.keep_alive_timeout = _KEEP_ALIVE_TIMEOUT
        await serve(
            asgi_app,
            config,
            mode="asgi",
            shutdown_trigger=lambda: _shutdown_trigger(stop_event),
        )

    asyncio.run(_serve())


def serve_tunnel(
    services: Iterable[Any],
    *,
    relay: RelayOptions,
    identity_keys: Optional[List[str]] = None,
    protocol: Optional[str] = None,
) -> TunnelHandle:
    """Boot a loopback-private Hypercorn (h2c) server for the given services and
    start a relay tunnel pointed at it.

    ``services`` accepts the same objects as :func:`restate.app` — services,
    virtual objects, or workflows from either the promise API or the codegen SDK.
    Returns a :class:`TunnelHandle` once the local server is listening and the
    engine has started; the receiver dials the relay in the background.
    """
    asgi_app = _restate_app(
        services=list(services),
        protocol=protocol,  # type: ignore[arg-type]
        identity_keys=identity_keys,
    )

    port = _free_loopback_port()
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_run_hypercorn,
        args=(asgi_app, port, stop_event),
        name="restate-relay-hypercorn",
        daemon=True,
    )
    thread.start()

    if not _wait_until_listening(port):
        stop_event.set()
        raise RuntimeError(f"relay tunnel: local Hypercorn server did not start listening on 127.0.0.1:{port}")

    config: dict[str, Any] = {
        "relay_addr": relay.address,
        "env": relay.env,
        "tunnel": relay.tunnel,
        "api_key": relay.api_key,
        "local_port": port,
    }
    if relay.connections is not None:
        config["connections"] = relay.connections
    if relay.instance_id is not None:
        config["instance_id"] = relay.instance_id

    try:
        native = _RelayTunnel.start(json.dumps(config))
    except BaseException:
        stop_event.set()
        thread.join(timeout=5.0)
        raise

    return TunnelHandle(native, port, stop_event, thread)
