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
Tests for `restate.connect()` and `python -m restate connect`: the Python layer, the
ASGI bridge and the engine, against a fake Restate Cloud tunnel peer speaking
plaintext HTTP/2 on localhost (role reversed: the peer is the HTTP/2 client).
"""

import asyncio
import base64
import hashlib
import json
import sys
import time
import typing

import pytest
from h2.config import H2Configuration
from h2.connection import ConnectionState, H2Connection
from h2.events import ConnectionTerminated, DataReceived, ResponseReceived, StreamEnded, StreamReset, TrailersReceived
from h2.exceptions import ProtocolError

import restate
import restate._internal as _internal  # pylint: disable=import-error,no-name-in-module
from restate.client import resolve_ingress
from restate.__main__ import _load_app

pytestmark = [pytest.mark.anyio]


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


# ───────────────────────────── ed25519 (RFC 8032, pure Python, tests only) ─────────────────────────────

_P = 2**255 - 19
_Q = 2**252 + 27742317777372353535851937790883648493


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


_D = (-121665 * _inv(121666)) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BY = 4 * _inv(5) % _P
_B = (_xrecover(_BY), _BY)


def _add(p: typing.Tuple[int, int], q: typing.Tuple[int, int]) -> typing.Tuple[int, int]:
    x1, y1 = p
    x2, y2 = q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _D * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _D * x1 * x2 * y1 * y2)
    return x3 % _P, y3 % _P


def _mul(p: typing.Tuple[int, int], e: int) -> typing.Tuple[int, int]:
    result = (0, 1)
    addend = p
    while e:
        if e & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        e >>= 1
    return result


def _encode_point(p: typing.Tuple[int, int]) -> bytes:
    x, y = p
    return ((y & ((1 << 255) - 1)) | ((x & 1) << 255)).to_bytes(32, "little")


def _clamp(h: bytes) -> int:
    a = int.from_bytes(h[:32], "little")
    return (a & ((1 << 254) - 8)) | (1 << 254)


def ed25519_public_key(seed: bytes) -> bytes:
    return _encode_point(_mul(_B, _clamp(hashlib.sha512(seed).digest())))


def ed25519_sign(message: bytes, seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = _clamp(h)
    public_key = _encode_point(_mul(_B, a))
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little") % _Q
    encoded_r = _encode_point(_mul(_B, r))
    k = int.from_bytes(hashlib.sha512(encoded_r + public_key + message).digest(), "little")
    s = (r + k * a) % _Q
    return encoded_r + s.to_bytes(32, "little")


# ───────────────────────────── keys ─────────────────────────────

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58_encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    digits = ""
    while number:
        number, remainder = divmod(number, 58)
        digits = _BASE58[remainder] + digits
    leading = len(data) - len(data.lstrip(b"\0"))
    return "1" * leading + digits


IDENTITY_SEED = bytes([0x2A] * 32)
SIGNING_PUBLIC_KEY = "publickeyv1_" + base58_encode(ed25519_public_key(IDENTITY_SEED))
ENVIRONMENT_ID = "env_test123"
REGION = "eu.restate.cloud"
API_KEY = "key_test.secret"
TUNNEL_NAME = "greeter-test"


def cloud_key(query: str) -> str:
    return "rstcloudv1_" + base58_encode(query.encode())


def connect_key(api_key: typing.Optional[str] = API_KEY) -> str:
    query = f"envid={ENVIRONMENT_ID}"
    if api_key:
        query += f"&apikey={api_key}"
    query += f"&region={REGION}&signingkey={SIGNING_PUBLIC_KEY}"
    return cloud_key(query)


def ingress_key() -> str:
    return cloud_key(f"envid={ENVIRONMENT_ID}&apikey={API_KEY}&region={REGION}")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def signed_identity(audience: str, seed: bytes = IDENTITY_SEED) -> str:
    now = int(time.time())
    header = {"alg": "EdDSA", "typ": "JWT", "kid": "publickeyv1_" + base58_encode(ed25519_public_key(seed))}
    claims = {"aud": audience, "nbf": now - 60, "iat": now, "exp": now + 60}
    signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(claims).encode())}"
    return f"{signing_input}.{b64url(ed25519_sign(signing_input.encode(), seed))}"


def identity_headers(audience: str) -> typing.List[typing.Tuple[str, str]]:
    return [
        ("accept", "application/vnd.restate.endpointmanifest.v2+json"),
        ("x-restate-signature-scheme", "v1"),
        ("x-restate-jwt-v1", signed_identity(audience)),
    ]


# ───────────────────────────── the fake Cloud peer ─────────────────────────────

TEST_TIMEOUT = 5.0


def _fail(future: "asyncio.Future[None]", error: BaseException) -> None:
    """Fail ``future``, which a test may no longer await: its exception is not one the loop must report."""
    future.set_exception(error)
    future.exception()


class Response:
    def __init__(self) -> None:
        self.stream_id = 0
        self.headers: typing.Dict[str, str] = {}
        self.trailers: typing.Dict[str, str] = {}
        self.trailer_pairs: typing.List[typing.Tuple[str, str]] = []
        self.body = bytearray()
        self.ended: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.started: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    @property
    def status(self) -> int:
        return int(self.headers[":status"])


class FakeCloud:
    """
    Accepts the SDK's outbound socket, runs HTTP/2 as the client, opens
    `GET /_/start-tunnel` and keeps its request body open until `establish()`
    sends the trailers. Then forwards requests over the same session.
    """

    def __init__(self) -> None:
        self.port = 0
        self._server: typing.Optional[asyncio.AbstractServer] = None
        self._conn: typing.Optional[H2Connection] = None
        self._writer: typing.Optional[asyncio.StreamWriter] = None
        self._responses: typing.Dict[int, Response] = {}
        self._handshake_stream = 0
        self._handshake: asyncio.Future[Response] = asyncio.get_running_loop().create_future()
        self._closed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._goaway: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._reader_task: typing.Optional[asyncio.Task[None]] = None

    async def __aenter__(self) -> "FakeCloud":
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # pylint: disable=broad-except
                pass
        if self._writer is not None:
            self._writer.close()
        if self._server is not None:
            self._server.close()
            await asyncio.wait_for(self._server.wait_closed(), TEST_TIMEOUT)

    @property
    def servers(self) -> typing.List[str]:
        return [f"http://127.0.0.1:{self.port}"]

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._conn is not None:
            writer.close()
            return
        conn = H2Connection(H2Configuration(client_side=True, header_encoding="utf-8"))
        conn.initiate_connection()
        self._conn = conn
        self._writer = writer
        stream_id = conn.get_next_available_stream_id()
        self._handshake_stream = stream_id
        response = Response()
        self._responses[stream_id] = response
        conn.send_headers(
            stream_id,
            [(":method", "GET"), (":path", "/_/start-tunnel"), (":scheme", "http"), (":authority", "fake")],
            end_stream=False,
        )
        await self._flush()
        self._reader_task = asyncio.create_task(self._read(reader, writer))

    async def _read(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert self._conn is not None
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                if self._goaway.done():
                    # python-h2 refuses every frame once it has received a GOAWAY, unlike
                    # a real peer, which keeps draining its streams. Keep the socket open
                    # and let the SDK decide when the connection ends.
                    continue
                try:
                    events = self._conn.receive_data(data)
                except ProtocolError:
                    # A GOAWAY followed by more frames in the same read: python-h2 raises
                    # on the frame after the GOAWAY and discards the events before it.
                    if self._conn.state_machine.state != ConnectionState.CLOSED:
                        raise
                    if not self._goaway.done():
                        self._goaway.set_result(None)
                    continue
                for event in events:
                    self._dispatch(event)
                await self._flush()
        finally:
            writer.close()
            if not self._closed.done():
                self._closed.set_result(None)
            for response in self._responses.values():
                if not response.ended.done():
                    _fail(response.ended, ConnectionError("session closed"))
                if not response.started.done():
                    _fail(response.started, ConnectionError("session closed"))

    def _dispatch(self, event: typing.Any) -> None:
        assert self._conn is not None
        if isinstance(event, ResponseReceived):
            response = self._responses[event.stream_id]
            response.headers = typing.cast(typing.Dict[str, str], dict(event.headers))
            if not response.started.done():
                response.started.set_result(None)
            if event.stream_id == self._handshake_stream and not self._handshake.done():
                self._handshake.set_result(response)
        elif isinstance(event, DataReceived):
            self._responses[event.stream_id].body.extend(event.data)
            self._conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
        elif isinstance(event, TrailersReceived):
            pairs = typing.cast(typing.List[typing.Tuple[str, str]], list(event.headers))
            self._responses[event.stream_id].trailers = dict(pairs)
            self._responses[event.stream_id].trailer_pairs = pairs
        elif isinstance(event, StreamEnded):
            response = self._responses[event.stream_id]
            if not response.ended.done():
                response.ended.set_result(None)
        elif isinstance(event, StreamReset):
            reset = self._responses.get(event.stream_id)
            if reset is not None and not reset.ended.done():
                _fail(reset.ended, ConnectionError(f"stream reset: {event.error_code}"))
        elif isinstance(event, ConnectionTerminated):
            if not self._goaway.done():
                self._goaway.set_result(None)

    async def _flush(self) -> None:
        assert self._conn is not None and self._writer is not None
        data = self._conn.data_to_send()
        if data:
            self._writer.write(data)
            await self._writer.drain()

    async def handshake(self) -> Response:
        """The SDK's answer to `GET /_/start-tunnel`: its credentials."""
        return await asyncio.wait_for(self._handshake, TEST_TIMEOUT)

    async def establish(self, name: str = TUNNEL_NAME, status: str = "ok") -> None:
        """Send the handshake trailers: the verdict."""
        assert self._conn is not None
        trailers = [("tunnel-status", status)]
        if status == "ok":
            trailers += [
                ("tunnel-name", name),
                ("proxy-url", f"https://proxy.example/{ENVIRONMENT_ID}/{name}"),
                ("tunnel-url", f"https://tunnel.example/{name}"),
            ]
        self._conn.send_headers(self._handshake_stream, trailers, end_stream=True)
        await self._flush()

    async def request(
        self,
        path: str,
        headers: typing.Optional[typing.List[typing.Tuple[str, str]]] = None,
        body: bytes = b"",
        method: str = "GET",
        wait: bool = True,
    ) -> Response:
        """Forward a request over the session; `path` is the raw forwarded path."""
        assert self._conn is not None
        stream_id = self._conn.get_next_available_stream_id()
        response = Response()
        response.stream_id = stream_id
        self._responses[stream_id] = response
        self._conn.send_headers(
            stream_id,
            [(":method", method), (":path", path), (":scheme", "http"), (":authority", "fake")] + (headers or []),
            end_stream=not body,
        )
        if body:
            self._conn.send_data(stream_id, body, end_stream=True)
        await self._flush()
        if wait:
            await asyncio.wait_for(response.ended, TEST_TIMEOUT)
        return response

    async def reset(self, stream_id: int) -> None:
        """Cancel a forwarded stream (RST_STREAM), as a timed-out invocation does."""
        assert self._conn is not None
        self._conn.reset_stream(stream_id)
        await self._flush()

    async def wait_closed(self) -> None:
        await asyncio.wait_for(self._closed, TEST_TIMEOUT)

    async def wait_goaway(self) -> None:
        """The SDK told this peer to stop opening streams (graceful shutdown)."""
        await asyncio.wait_for(self._goaway, TEST_TIMEOUT)


# ───────────────────────────── helpers ─────────────────────────────

greeter = restate.Service("Greeter")


@greeter.handler()
async def greet(ctx: restate.Context, name: str) -> str:
    return f"Hello {name}"


def options_for(cloud: FakeCloud, **kwargs: typing.Any) -> typing.Any:
    return _internal.TunnelEngineOptions.resolve(
        name=kwargs.pop("name", TUNNEL_NAME),
        key=kwargs.pop("key", connect_key()),
        servers=cloud.servers,
        worker_id="worker-test",
        **kwargs,
    )


async def established(cloud: FakeCloud, app: typing.Any = None) -> typing.Tuple[typing.Any, typing.Any]:
    app = app or restate.app(services=[greeter])
    engine = _internal.TunnelEngine(app, options_for(cloud))
    ready = asyncio.ensure_future(engine.ready())
    handshake = await cloud.handshake()
    assert handshake.status == 200
    assert handshake.headers["authorization"] == f"Bearer {API_KEY}"
    assert handshake.headers["environment-id"] == ENVIRONMENT_ID
    assert handshake.headers["tunnel-name"] == TUNNEL_NAME
    assert handshake.headers["tunnel-worker-id"] == "worker-test"
    await asyncio.sleep(0)
    assert not ready.done(), "ready completed before the handshake trailers"
    await cloud.establish()
    info = await asyncio.wait_for(ready, TEST_TIMEOUT)
    return engine, info


# ───────────────────────────── configuration ─────────────────────────────


def test_options_resolve_from_arguments_and_errors(monkeypatch: pytest.MonkeyPatch):
    for name in ("RESTATE_CONNECT_KEY", "RESTATE_CONNECT_NAME", "RESTATE_INPROC_ENVIRONMENT_ID"):
        monkeypatch.delenv(name, raising=False)

    options = _internal.TunnelEngineOptions.resolve(name="greeter", key=connect_key())
    assert options.name == "greeter"
    assert options.environment_id == ENVIRONMENT_ID
    assert options.region == REGION
    assert options.source == "code"
    assert options.registration_identifier == "connect://greeter"
    assert options.registration_deep_link.endswith("register-deployment=connect://greeter")
    assert "secret" not in repr(options)

    with pytest.raises(_internal.ConnectError, match="needs a name"):
        _internal.TunnelEngineOptions.resolve(key=connect_key())
    with pytest.raises(_internal.ConnectError, match="RESTATE_CONNECT_KEY"):
        _internal.TunnelEngineOptions.resolve(name="greeter")
    with pytest.raises(_internal.ConnectError, match="the key option is not a Restate Cloud key"):
        _internal.TunnelEngineOptions.resolve(name="greeter", key="key_nope")
    with pytest.raises(_internal.ConnectError, match="carries no API key"):
        _internal.TunnelEngineOptions.resolve(name="greeter", key=connect_key(api_key=None))
    with pytest.raises(_internal.ConnectError, match="carries no signing key"):
        _internal.TunnelEngineOptions.resolve(name="greeter", key=ingress_key())
    with pytest.raises(ValueError, match="unknown tuning option"):
        _internal.TunnelEngineOptions.resolve(name="greeter", key=connect_key(), tuning={"nope": 1})

    monkeypatch.setenv("RESTATE_CONNECT_KEY", connect_key())
    monkeypatch.setenv("RESTATE_CONNECT_NAME", "from-env")
    options = _internal.TunnelEngineOptions.resolve()
    assert options.name == "from-env"
    assert options.source == "connect_key"


def test_ingress_key_and_client_resolution(monkeypatch: pytest.MonkeyPatch):
    key = _internal.IngressKey.parse(ingress_key())
    assert key.environment_id == ENVIRONMENT_ID
    assert key.ingress_url() == f"https://test123.env.{REGION}:8080"
    assert key.bearer() == f"Bearer {API_KEY}"
    assert "secret" not in repr(key)
    with pytest.raises(_internal.ConnectError, match="RESTATE_INGRESS_KEY is not a Restate Cloud key"):
        _internal.IngressKey.parse("nope", "RESTATE_INGRESS_KEY")

    monkeypatch.delenv("RESTATE_INGRESS_KEY", raising=False)
    assert _internal.IngressKey.from_env() is None
    assert resolve_ingress("http://localhost:8080", None) == ("http://localhost:8080", None)
    with pytest.raises(_internal.ConnectError, match="RESTATE_INGRESS_KEY"):
        resolve_ingress(None, None)

    monkeypatch.setenv("RESTATE_INGRESS_KEY", ingress_key())
    url, headers = resolve_ingress(None, {"x-extra": "1"})
    assert url == f"https://test123.env.{REGION}:8080"
    assert headers == {"x-extra": "1", "Authorization": f"Bearer {API_KEY}"}
    _, headers = resolve_ingress(None, {"authorization": "Bearer mine"})
    assert headers == {"authorization": "Bearer mine"}


def test_load_app_resolves_module_and_attribute():
    assert _load_app("restate:app") is restate.app
    assert _load_app("restate.endpoint") is restate.app
    with pytest.raises(ValueError, match="no attribute"):
        _load_app("restate.connection:missing")
    with pytest.raises(ValueError, match="cannot import"):
        _load_app("definitely_not_a_module")


async def test_engine_requires_a_running_loop():
    options = _internal.TunnelEngineOptions.resolve(name="greeter", key=connect_key(), servers=["http://127.0.0.1:1"])

    def outside() -> None:
        with pytest.raises(RuntimeError, match="event loop"):
            _internal.TunnelEngine(restate.app(services=[]), options)

    await asyncio.get_running_loop().run_in_executor(None, outside)


# ───────────────────────────── the engine through the ASGI bridge ─────────────────────────────


async def test_handshake_then_forwarded_discovery():
    async with FakeCloud() as cloud:
        engine, info = await established(cloud)
        assert info.name == TUNNEL_NAME
        assert info.deployment_url == f"https://proxy.example:9080/{ENVIRONMENT_ID}/{TUNNEL_NAME}/http/in-process/9080/"

        response = await cloud.request("/http/h/9080/discover?x=1", identity_headers("/discover"))
        assert response.status == 200
        assert response.headers["content-type"].startswith("application/vnd.restate.endpointmanifest")
        assert response.headers["x-restate-server"].startswith("restate-sdk-python/")
        manifest = json.loads(bytes(response.body))
        assert manifest["protocolMode"] == "BIDI_STREAM"
        assert [service["name"] for service in manifest["services"]] == ["Greeter"]

        # Answered by the engine, unsigned.
        assert (await cloud.request("/_/health")).status == 200
        # Identity is verified by the engine before the app sees the request.
        assert (await cloud.request("/http/h/9080/discover", [("accept", "application/json")])).status == 401
        assert (await cloud.request("/http/h/9080/discover", identity_headers("/http/h/9080/discover"))).status == 401
        # The app's own routing after the rewrite.
        assert (await cloud.request("/http/h/9080/restate/health", identity_headers("/restate/health"))).status == 200
        assert (await cloud.request("/http/h/9080/nope", identity_headers("/nope"))).status == 404

        await engine.shutdown(0.0)
        await cloud.wait_closed()
        await engine.terminated()


async def test_invocation_reaches_the_handler_route(caplog: pytest.LogCaptureFixture):
    async with FakeCloud() as cloud:
        engine, _ = await established(cloud)
        # An invocation without the service-protocol content type stops at the SDK's media
        # type check, which raises inside the app: the request reached the handler route
        # through the bridge, and an app exception is a 500 (as under any ASGI server).
        response = await cloud.request(
            "/http/h/9080/invoke/Greeter/greet",
            identity_headers("/invoke/Greeter/greet") + [("content-type", "application/json")],
            body=b'"world"',
            method="POST",
        )
        assert response.status == 500
        assert any("415" in record.getMessage() or "415" in str(record.exc_info) for record in caplog.records)
        await engine.shutdown(0.0)


async def test_app_exceptions_become_500_and_resets():
    calls: typing.List[str] = []

    async def failing_app(scope: typing.Any, receive: typing.Any, send: typing.Any) -> None:
        calls.append(scope["path"])
        assert scope["type"] == "http"
        assert scope["http_version"] == "2"
        assert scope["scheme"] == "https"
        assert "http.response.trailers" in scope["extensions"]
        assert (b"x-restate-signature-scheme", b"v1") in scope["headers"]
        event = await receive()
        assert event == {"type": "http.request", "body": b"", "more_body": False}
        if scope["path"] == "/my path":
            assert scope["query_string"] == b"a=1"
            assert scope["raw_path"] == b"/my%20path"
            raise RuntimeError("boom before start")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise RuntimeError("boom after start")

    async with FakeCloud() as cloud:
        engine, _ = await established(cloud, failing_app)
        # The identity audience is the raw forwarded path, as Restate signs it.
        response = await cloud.request("/http/h/9080/my%20path?a=1", identity_headers("/my%20path"))
        assert response.status == 500

        response = await cloud.request("/http/h/9080/other", identity_headers("/other"), wait=False)
        await asyncio.wait_for(response.started, TEST_TIMEOUT)
        assert response.status == 200
        with pytest.raises(ConnectionError, match="stream reset"):
            await asyncio.wait_for(response.ended, TEST_TIMEOUT)
        assert calls == ["/my path", "/other"]
        await engine.shutdown(0.0)


async def test_send_validation_and_trailers():
    errors: typing.List[str] = []

    async def expect_error(coro: typing.Awaitable[None]) -> None:
        try:
            await coro
            errors.append("no error")
        except RuntimeError as error:
            errors.append(str(error))

    async def app(scope: typing.Any, receive: typing.Any, send: typing.Any) -> None:
        if scope["path"] == "/bad-order":
            await expect_error(send({"type": "http.response.body", "body": b"x"}))
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await expect_error(send({"type": "http.response.start", "status": 200, "headers": []}))
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            # Even after the stream completed, the protocol is still enforced.
            await asyncio.sleep(0.05)
            await expect_error(send({"type": "http.response.body", "body": b""}))
            return
        if scope["path"] == "/trailers":
            await send({"type": "http.response.start", "status": 200, "headers": [(b"x-a", b"1")], "trailers": True})
            for chunk in (b"one", b"two", b"three"):
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            await send({"type": "http.response.trailers", "headers": [(b"x-t", b"1")], "more_trailers": True})
            await send({"type": "http.response.trailers", "headers": [(b"x-u", b"2"), (b"x-t", b"3"), (b"x-t", b"4")]})
            # The response is complete: receive reports the disconnect.
            assert (await receive())["type"] == "http.disconnect"
            return
        raise AssertionError(scope["path"])

    async with FakeCloud() as cloud:
        engine, _ = await established(cloud, app)
        response = await cloud.request("/http/h/9080/bad-order", identity_headers("/bad-order"))
        assert response.status == 204
        await asyncio.sleep(0.2)
        assert [e.split(",")[0] for e in errors] == [
            "Unexpected ASGI message 'http.response.body'",
            "Unexpected ASGI message 'http.response.start'",
            "Unexpected ASGI message 'http.response.body'",
        ], errors
        assert "no further message" in errors[2]

        response = await cloud.request("/http/h/9080/trailers", identity_headers("/trailers"))
        assert response.status == 200
        assert response.headers["x-a"] == "1"
        assert bytes(response.body) == b"onetwothree"
        assert response.trailer_pairs == [("x-t", "1"), ("x-t", "3"), ("x-t", "4"), ("x-u", "2")]
        await engine.shutdown(0.0)


async def test_request_body_is_streamed_and_disconnect_follows_completion():
    seen: typing.List[typing.Any] = []

    async def app(scope: typing.Any, receive: typing.Any, send: typing.Any) -> None:
        while True:
            event = await receive()
            seen.append(event)
            if event["type"] == "http.request" and not event["more_body"]:
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})
        seen.append(await receive())
        # After the disconnect, sends are accepted and dropped.
        await send({"type": "http.response.body", "body": b"late"})

    async with FakeCloud() as cloud:
        engine, _ = await established(cloud, app)
        response = await cloud.request("/http/h/9080/echo", identity_headers("/echo"), body=b"payload", method="POST")
        assert response.status == 200
        assert bytes(response.body) == b"ok"
        await asyncio.sleep(0.1)
        assert seen[0] == {"type": "http.request", "body": b"payload", "more_body": True}
        assert seen[1] == {"type": "http.request", "body": b"", "more_body": False}
        assert seen[2] == {"type": "http.disconnect"}
        await engine.shutdown(0.0)


async def test_reset_before_the_response_head_delivers_disconnect():
    # A stream cancelled while the app has consumed the request but not yet started the
    # response (hyper drops the request future): receive() must still report the disconnect.
    outcome: asyncio.Future[typing.Any] = asyncio.get_running_loop().create_future()
    consumed = asyncio.Event()
    proceed = asyncio.Event()

    async def app(scope: typing.Any, receive: typing.Any, send: typing.Any) -> None:
        assert (await receive())["more_body"] is False
        consumed.set()
        await proceed.wait()
        try:
            event = await asyncio.wait_for(receive(), TEST_TIMEOUT)
            outcome.set_result(event)
        except BaseException as error:  # pylint: disable=broad-except
            outcome.set_exception(error)
            raise
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"late", "more_body": False})

    async with FakeCloud() as cloud:
        engine, _ = await established(cloud, app)
        response = await cloud.request("/http/h/9080/slow-start", identity_headers("/slow-start"), wait=False)
        await asyncio.wait_for(consumed.wait(), TEST_TIMEOUT)
        await cloud.reset(response.stream_id)
        await asyncio.sleep(0.1)
        proceed.set()
        assert await asyncio.wait_for(outcome, TEST_TIMEOUT) == {"type": "http.disconnect"}
        # The late response is dropped silently and the engine is unaffected.
        assert (await cloud.request("/_/health")).status == 200
        await engine.shutdown(0.0)


class StallingCloud(FakeCloud):
    """A peer that never opens its flow-control window: the SDK's body frames queue up."""

    def __init__(self) -> None:
        super().__init__()
        self.pending: typing.List[typing.Tuple[int, int]] = []

    async def __aenter__(self) -> "StallingCloud":
        await super().__aenter__()
        return self

    def _dispatch(self, event: typing.Any) -> None:
        if isinstance(event, DataReceived):
            self._responses[event.stream_id].body.extend(event.data)
            self.pending.append((event.flow_controlled_length, event.stream_id))
            return
        super()._dispatch(event)

    async def open_window(self) -> None:
        assert self._conn is not None
        for length, stream_id in self.pending:
            self._conn.acknowledge_received_data(length, stream_id)
        self.pending.clear()
        await self._flush()


async def test_app_failure_with_a_buffered_frame_resets_the_stream():
    # The peer stalls; the app has frames queued in hyper and in the bridge when it
    # raises. The stream must end with a reset, never as a complete response.
    raised = asyncio.Event()

    async def app(scope: typing.Any, receive: typing.Any, send: typing.Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"a" * 70000, "more_body": True})
        await send({"type": "http.response.body", "body": b"b" * 10, "more_body": True})
        await send({"type": "http.response.body", "body": b"c" * 10, "more_body": True})
        raised.set()
        raise RuntimeError("boom with a buffered frame")

    async with StallingCloud() as cloud:
        engine, _ = await established(cloud, app)
        response = await cloud.request("/http/h/9080/stall", identity_headers("/stall"), wait=False)
        await asyncio.wait_for(raised.wait(), TEST_TIMEOUT)
        await asyncio.sleep(0.2)
        await cloud.open_window()
        with pytest.raises(ConnectionError, match="stream reset"):
            await asyncio.wait_for(response.ended, TEST_TIMEOUT)
        await engine.shutdown(0.0)


async def test_graceful_shutdown_waits_for_in_flight_invocations():
    # The drain protocol itself (sentinel for raced streams, flushing, the deadline) is
    # covered by the engine's own tests; python-h2 cannot speak after a GOAWAY, so here
    # only the observable contract is checked: shutdown waits for the in-flight
    # invocation, tells the peer to stop, and the grace deadline bounds the wait.
    release = asyncio.Event()
    completed = asyncio.Event()

    async def app(scope: typing.Any, receive: typing.Any, send: typing.Any) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await release.wait()
        await send({"type": "http.response.body", "body": b"done", "more_body": False})
        completed.set()

    async with FakeCloud() as cloud:
        engine, _ = await established(cloud, app)
        in_flight = await cloud.request("/http/h/9080/slow", identity_headers("/slow"), wait=False)
        await asyncio.wait_for(in_flight.started, TEST_TIMEOUT)

        started_at = time.monotonic()
        shutdown = asyncio.ensure_future(engine.shutdown(1.5))
        await cloud.wait_goaway()
        await asyncio.sleep(0.2)
        assert not shutdown.done(), "shutdown completed while an invocation was in flight"

        release.set()
        await asyncio.wait_for(completed.wait(), TEST_TIMEOUT)
        await asyncio.wait_for(shutdown, TEST_TIMEOUT)
        # The peer never acknowledged the shutdown ping, so the deadline ended the wait.
        assert 1.0 <= time.monotonic() - started_at <= 4.0
        await cloud.wait_closed()
        await engine.terminated()


async def test_rejected_credentials_are_fatal():
    async with FakeCloud() as cloud:
        engine = _internal.TunnelEngine(restate.app(services=[greeter]), options_for(cloud))
        ready = asyncio.ensure_future(engine.ready())
        await cloud.handshake()
        await cloud.establish(status="unauthorized")
        with pytest.raises(_internal.ConnectError, match="unauthorized"):
            await asyncio.wait_for(ready, TEST_TIMEOUT)
        with pytest.raises(_internal.ConnectError, match="unauthorized"):
            await engine.terminated()


async def test_dropping_the_engine_closes_the_connection():
    async with FakeCloud() as cloud:
        engine, _ = await established(cloud)
        del engine
        await cloud.wait_closed()


# ───────────────────────────── restate.connect() and Connection ─────────────────────────────


class OptionsWithServers:
    """Stands in for `TunnelEngineOptions` in `restate.connection`, pointing at the fake peer."""

    def __init__(self, cloud: FakeCloud, **tuning: typing.Any) -> None:
        self._cloud = cloud
        self._tuning = tuning

    def resolve(self, **kwargs: typing.Any) -> typing.Any:
        return _internal.TunnelEngineOptions.resolve(
            servers=self._cloud.servers, worker_id="worker-test", tuning=self._tuning or None, **kwargs
        )


async def test_with_connection(monkeypatch: pytest.MonkeyPatch):
    async with FakeCloud() as cloud:
        monkeypatch.setattr(sys.modules["restate.connection"], "TunnelEngineOptions", OptionsWithServers(cloud))

        async def run() -> None:
            async with restate.with_connection(
                restate.app(services=[greeter]), name=TUNNEL_NAME, key=connect_key()
            ) as conn:
                assert conn.name == TUNNEL_NAME
                assert conn.registration_identifier == f"connect://{TUNNEL_NAME}"
                assert conn.info.name == TUNNEL_NAME
                response = await cloud.request("/http/h/9080/discover", identity_headers("/discover"))
                assert response.status == 200
                waiter = asyncio.ensure_future(conn.wait_closed())
                await asyncio.sleep(0.05)
                assert not waiter.done()
                await conn.shutdown(0.0)
                await asyncio.wait_for(waiter, TEST_TIMEOUT)
                # Leaving the block shuts down again: a no-op that never raises.

        task = asyncio.ensure_future(run())
        await cloud.handshake()
        await cloud.establish()
        await asyncio.wait_for(task, TEST_TIMEOUT)
        await cloud.wait_closed()


async def test_with_connection_leaves_the_block_with_a_drain(monkeypatch: pytest.MonkeyPatch):
    async with FakeCloud() as cloud:
        monkeypatch.setattr(sys.modules["restate.connection"], "TunnelEngineOptions", OptionsWithServers(cloud))

        async def run() -> None:
            async with restate.with_connection(restate.app(services=[greeter]), name=TUNNEL_NAME, key=connect_key()):
                pass

        task = asyncio.ensure_future(run())
        await cloud.handshake()
        await cloud.establish()
        # Leaving the block drains: the peer is told to stop opening streams.
        await cloud.wait_goaway()
        assert not task.done()
    # python-h2 cannot finish the drain handshake; closing the peer ends the connection.
    await asyncio.wait_for(task, TEST_TIMEOUT)


async def test_connection_reports_configuration_errors(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("RESTATE_CONNECT_KEY", raising=False)
    monkeypatch.delenv("RESTATE_INPROC_ENVIRONMENT_ID", raising=False)
    app = restate.app(services=[greeter])
    with pytest.raises(restate.ConnectError, match="RESTATE_CONNECT_KEY"):
        async with restate.with_connection(app, name="greeter"):
            pass
    with pytest.raises(restate.ConnectError, match="RESTATE_CONNECT_KEY"):
        await restate.connect(app, name="greeter")


async def test_with_connection_fails_fast_after_a_fatal_error(monkeypatch: pytest.MonkeyPatch):
    async with FakeCloud() as cloud:
        monkeypatch.setattr(sys.modules["restate.connection"], "TunnelEngineOptions", OptionsWithServers(cloud))

        async def run() -> None:
            async with restate.with_connection(restate.app(services=[greeter]), name=TUNNEL_NAME, key=connect_key()):
                raise AssertionError("must not enter")

        task = asyncio.ensure_future(run())
        await cloud.handshake()
        await cloud.establish(status="unauthorized")
        with pytest.raises(restate.ConnectError, match="unauthorized"):
            await asyncio.wait_for(task, TEST_TIMEOUT)


async def test_connect_runs_until_the_connection_stops(monkeypatch: pytest.MonkeyPatch):
    # The fake peer going away (with a reconnect policy that gives up at once) is what
    # stops connect() here, as a ConnectError; signals are covered manually.
    async with FakeCloud() as cloud:
        monkeypatch.setattr(
            sys.modules["restate.connection"],
            "TunnelEngineOptions",
            OptionsWithServers(cloud, reconnect_max_attempts=1),
        )
        task = asyncio.ensure_future(
            restate.connect(restate.app(services=[greeter]), name=TUNNEL_NAME, key=connect_key())
        )
        await cloud.handshake()
        await cloud.establish()
        response = await cloud.request("/http/h/9080/discover", identity_headers("/discover"))
        assert response.status == 200
        assert not task.done()

    # Leaving the block closes the peer: the engine gives up and connect() raises.
    with pytest.raises(restate.ConnectError, match="max_attempts"):
        await asyncio.wait_for(task, TEST_TIMEOUT)
