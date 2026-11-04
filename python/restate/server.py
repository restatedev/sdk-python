#
#  Copyright (c) 2023-2024 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""This module contains the ASGI server for the restate framework."""

import asyncio
import functools
import logging
import signal
import weakref
from types import FrameType
from typing import Callable, Dict, List, Optional, Set, TypedDict, Literal

from restate.discovery import compute_discovery_json
from restate.endpoint import Endpoint
from restate.entry_codec import JournalValueCodec
from restate.server_context import ServerInvocationContext, DisconnectedException
from restate.server_types import Receive, ReceiveChannel, RestateAppT, Scope, Send, binary_to_header, header_to_binary  # pylint: disable=line-too-long
from restate.vm import VMWrapper
from restate.aws_lambda import is_running_on_lambda, wrap_asgi_as_lambda_handler

from restate._internal import PyIdentityVerifier, IdentityVerificationException  # pylint: disable=import-error,no-name-in-module
from restate._internal import SDK_VERSION  # pylint: disable=import-error,no-name-in-module

logger = logging.getLogger(__name__)

# Shutdown hooks, keyed by event loop and signal. asyncio keeps one handler per signal, so
# everything that wants to react to SIGTERM (the ASGI app, `python -m restate connect`)
# registers a hook here and a single dispatcher is installed per loop.
_shutdown_hooks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Dict[int, List[Callable[[], None]]]]" = (
    weakref.WeakKeyDictionary()
)


def install_shutdown_hook(
    loop: asyncio.AbstractEventLoop,
    hook: Callable[[], None],
    signum: int = signal.SIGTERM,
    *,
    chain: bool = False,
) -> None:
    """
    Run ``hook`` when ``signum`` (default SIGTERM) is received while ``loop`` runs.

    All hooks of a loop and signal share one signal handler, which the first hook installs:

    - By default, with ``loop.add_signal_handler``. That replaces whatever handler the process
      had for ``signum``, which is right for a program that owns its signals, such as
      ``restate.connect``.
    - With ``chain=True``, as a handler that also calls the process's previous handler. That is
      right for an app run by an ASGI server (uvicorn, hypercorn...): the server has installed
      its own handler to shut down, and replacing it would keep the server running.

    Raises what ``loop.add_signal_handler`` or ``signal.signal`` raise when signals cannot be
    handled here (Windows, or a non-main thread).
    """
    hooks = _shutdown_hooks.setdefault(loop, {})
    if signum not in hooks:
        registered: List[Callable[[], None]] = []

        def dispatch() -> None:
            for registered_hook in list(registered):
                try:
                    registered_hook()
                except Exception:  # pylint: disable=broad-except
                    logger.exception("Shutdown hook failed")

        if chain:
            _install_chained_handler(loop, signum, dispatch)
        else:
            loop.add_signal_handler(signum, dispatch)
        hooks[signum] = registered
    hooks[signum].append(hook)


def _install_chained_handler(loop: asyncio.AbstractEventLoop, signum: int, dispatch: Callable[[], None]) -> None:
    """
    Install a ``signal.signal`` handler for ``signum`` that schedules ``dispatch`` on ``loop``, and then
    calls the handler it replaced.

    This keeps both kinds of server handler working:

    - one installed with ``signal.signal`` (uvicorn's) is the previous handler, which this calls;
    - one installed with ``loop.add_signal_handler`` (hypercorn's) keeps running regardless, because
      asyncio delivers those through the loop's wakeup fd, not through the Python-level handler.

    Python's default SIGINT handler is not called: it would raise ``KeyboardInterrupt`` into
    whatever code happens to be running.
    """
    previous = signal.getsignal(signum)

    def handler(received: int, frame: Optional[FrameType]) -> None:
        if not loop.is_closed():
            loop.call_soon_threadsafe(dispatch)
        if callable(previous) and previous is not signal.default_int_handler:
            previous(received, frame)

    signal.signal(signum, handler)


X_RESTATE_SERVER = header_to_binary([("x-restate-server", f"restate-sdk-python/{SDK_VERSION}")])


async def send_status(send, receive, status_code: int):
    """respond with a status code"""
    await send({"type": "http.response.start", "status": status_code, "headers": X_RESTATE_SERVER})
    # For more info on why this loop, see ServerInvocationContext.leave()
    # pylint: disable=R0801
    while True:
        event = await receive()
        if event is None:
            break
        if event.get("type") == "http.disconnect":
            break
        if event.get("type") == "http.request" and event.get("more_body", False) is False:
            break
    await send({"type": "http.response.body"})


async def send404(send, receive):
    """respond with a 404"""
    await send_status(send, receive, 404)


async def send_discovery(scope: Scope, send: Send, endpoint: Endpoint):
    """respond with a discovery"""
    discovered_as: Literal["request_response", "bidi"]
    if scope["http_version"] == "1.1":
        discovered_as = "request_response"
    else:
        discovered_as = "bidi"

    # Extract Accept header from request
    accept_header = None
    for header_name, header_value in binary_to_header(scope["headers"]):
        if header_name.lower() == "accept":
            accept_header = header_value
            break

    # Negotiate discovery protocol version
    version = 2
    if accept_header:
        if "application/vnd.restate.endpointmanifest.v5+json" in accept_header:
            version = 5
        elif "application/vnd.restate.endpointmanifest.v4+json" in accept_header:
            version = 4
        elif "application/vnd.restate.endpointmanifest.v3+json" in accept_header:
            version = 3
        elif "application/vnd.restate.endpointmanifest.v2+json" in accept_header:
            version = 2
        else:
            await send_status_with_error_text(send, 415, f"Unsupported discovery version {accept_header}")
            return

    try:
        js = compute_discovery_json(endpoint, version, discovered_as)
        bin_headers = header_to_binary([("content-type", f"application/vnd.restate.endpointmanifest.v{version}+json")])
        bin_headers.extend(X_RESTATE_SERVER)
        await send({"type": "http.response.start", "status": 200, "headers": bin_headers, "trailers": False})
        await send(
            {
                "type": "http.response.body",
                "body": js.encode("utf-8"),
                "more_body": False,
            }
        )
    except ValueError as e:
        await send_status_with_error_text(send, 500, f"Error when computing discovery {e}")
        return


async def send_status_with_error_text(send: Send, status_code: int, error_text: str):
    """respond with an health check"""
    headers = header_to_binary([("content-type", "text/plain")])
    headers.extend(X_RESTATE_SERVER)
    await send({"type": "http.response.start", "status": status_code, "headers": headers, "trailers": False})
    await send(
        {
            "type": "http.response.body",
            "body": error_text.encode("utf-8"),
            "more_body": False,
        }
    )


async def send_health_check(send: Send):
    """respond with an health check"""
    headers = header_to_binary([("content-type", "application/json")])
    headers.extend(X_RESTATE_SERVER)
    await send({"type": "http.response.start", "status": 200, "headers": headers, "trailers": False})
    await send(
        {
            "type": "http.response.body",
            "body": b'{"status":"ok"}',
            "more_body": False,
        }
    )


async def process_invocation_to_completion(
    vm: VMWrapper,
    handler,
    attempt_headers: Dict[str, str],
    receive: ReceiveChannel,
    send: Send,
    journal_codec: Optional[JournalValueCodec] = None,
):
    """Invoke the user code."""
    status, res_headers = vm.get_response_head()
    res_bin_headers = header_to_binary(res_headers)
    res_bin_headers.extend(X_RESTATE_SERVER)
    await send({"type": "http.response.start", "status": status, "headers": res_bin_headers, "trailers": False})
    assert status == 200
    # ========================================
    # Read the input and the journal
    # ========================================
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            # everything ends here really ...
            return
        if message.get("type") == "http.request":
            body = message.get("body", None)
            assert isinstance(body, bytes)
            vm.notify_input(body)
        if not message.get("more_body", False):
            vm.notify_input_closed()
            break
        if vm.is_ready_to_execute():
            break
    # ========================================
    # Execute the user code
    # ========================================
    invocation = vm.sys_input()
    context = ServerInvocationContext(
        vm=vm,
        handler=handler,
        invocation=invocation,
        attempt_headers=attempt_headers,
        send=send,
        receive=receive,
        journal_codec=journal_codec,
    )
    try:
        await context.enter()
    except asyncio.exceptions.CancelledError:
        context.on_attempt_finished()
        raise
    except DisconnectedException:
        # The client disconnected before we could send the response
        context.on_attempt_finished()
        return
    # pylint: disable=W0718
    except Exception:
        logger.exception("Exception in Restate handler")
    try:
        await context.leave()
    finally:
        context.on_attempt_finished()


class LifeSpanNotImplemented(ValueError):
    """Signal to the asgi server that we didn't implement lifespans"""


class ParsedPath(TypedDict):
    """Parsed path from the request."""

    type: Literal["invocation", "health", "discover", "unknown"]
    service: str | None
    handler: str | None


def parse_path(request: str) -> ParsedPath:
    """Parse the path from the request."""
    # The following routes are possible
    # $mountpoint/health
    # $mountpoint/discover
    # $mountpoint/invoke/:service/:handler
    # as we don't know the mountpoint, we need to check the path carefully
    fragments = request.rsplit("/", 4)
    # /invoke/:service/:handler
    if len(fragments) >= 3 and fragments[-3] == "invoke":
        return {"type": "invocation", "handler": fragments[-1], "service": fragments[-2]}
    # /health
    if fragments[-1] == "health":
        return {"type": "health", "service": None, "handler": None}
    # /discover
    if fragments[-1] == "discover":
        return {"type": "discover", "service": None, "handler": None}
    # anything other than invoke is 404
    return {"type": "unknown", "service": None, "handler": None}


def asgi_app(endpoint: Endpoint) -> RestateAppT:
    """Create an ASGI-3 app for the given endpoint."""

    # Prepare request signer
    identity_verifier = PyIdentityVerifier(endpoint.identity_keys)

    active_channels: Set[ReceiveChannel] = set()
    sigterm_installed = False

    # Journal value codec resolution.
    # A codec (or an async provider building one) may be configured on the endpoint. Whether one is
    # configured is known synchronously and drives the VM's payload-checks flag. An async provider
    # is resolved once, lazily on the first invocation, and the resulting codec is reused.
    codec_config = endpoint.journal_value_codec
    disable_payload_checks = codec_config is not None

    # Memoize the provider's resolution. functools.cache can't wrap the async provider directly
    # (it would cache a coroutine, which can't be awaited twice), so we cache a sync factory that
    # returns the Task instead: the provider runs once and every caller awaits the same Task.
    @functools.cache
    def _codec_task() -> "asyncio.Task[JournalValueCodec]":
        assert callable(codec_config)
        return asyncio.ensure_future(codec_config())

    async def get_codec() -> Optional[JournalValueCodec]:
        if codec_config is None or isinstance(codec_config, JournalValueCodec):
            # Nothing configured, or an already-built instance: nothing to await.
            return codec_config
        return await _codec_task()

    def _on_sigterm() -> None:
        """Notify all active receive channels of graceful shutdown."""
        for ch in active_channels:
            ch.notify_shutdown()

    async def app(scope: Scope, receive: Receive, send: Send):
        nonlocal sigterm_installed
        if not sigterm_installed:
            loop = asyncio.get_running_loop()
            try:
                # Chained: the ASGI server running this app has its own SIGTERM handler, to shut down.
                install_shutdown_hook(loop, _on_sigterm, signal.SIGTERM, chain=True)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # Windows or non-main thread
            sigterm_installed = True
        try:
            if scope["type"] == "lifespan":
                raise LifeSpanNotImplemented()
            if scope["type"] != "http":
                raise NotImplementedError(f"Unknown scope type {scope['type']}")

            request_path = scope["path"]
            assert isinstance(request_path, str)
            request: ParsedPath = parse_path(request_path)

            # Health check
            if request["type"] == "health":
                await send_health_check(send)
                return

            # Verify Identity
            assert not isinstance(scope["headers"], str)
            assert hasattr(scope["headers"], "__iter__")
            request_headers = binary_to_header(scope["headers"])
            try:
                identity_verifier.verify(request_headers, request_path)
            except IdentityVerificationException:
                # Identify verification failed, send back unauthorized and close
                await send_status(send, receive, 401)
                return

            # might be a discovery request
            if request["type"] == "discover":
                await send_discovery(scope, send, endpoint)
                return
            # anything other than invoke is 404
            if request["type"] == "unknown":
                await send404(send, receive)
                return
            assert request["type"] == "invocation"
            assert request["service"] is not None
            assert request["handler"] is not None
            service_name, handler_name = request["service"], request["handler"]
            service = endpoint.services.get(service_name)
            if not service:
                await send404(send, receive)
                return
            handler = service.handlers.get(handler_name)
            if not handler:
                await send404(send, receive)
                return
            #
            # At this point we have a valid handler.
            # Let us set up restate's execution context for this invocation and handler.
            #
            receive_channel = ReceiveChannel(receive)
            active_channels.add(receive_channel)
            try:
                journal_codec = await get_codec()
                await process_invocation_to_completion(
                    VMWrapper(
                        request_headers,
                        disable_payload_checks=disable_payload_checks,
                        explicit_recovery=handler.explicit_recovery or service.explicit_recovery,
                        explicit_cancellation=handler.explicit_cancellation or service.explicit_cancellation,
                    ),
                    handler,
                    dict(request_headers),
                    receive_channel,
                    send,
                    journal_codec=journal_codec,
                )
            finally:
                active_channels.discard(receive_channel)
                await receive_channel.close()
        except LifeSpanNotImplemented as e:
            raise e
        except Exception as e:
            logger.exception("Exception in ASGI app")
            raise e

    if is_running_on_lambda():
        # If we're on Lambda, just return the adapter
        return wrap_asgi_as_lambda_handler(app)

    return app
