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
"""Tests for optional ingress HTTP clients."""

import builtins
from dataclasses import dataclass
from datetime import timedelta
import subprocess
import sys
from types import ModuleType
import typing
from unittest.mock import Mock

import httpx
import httpx2
import pytest

import restate
from restate import HttpClient, HttpError, HttpResponse, JournalValueCodec
from restate.client import Client, create_client
from restate.serde import JsonSerde


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


pytestmark = pytest.mark.anyio


@dataclass
class StubResponse:
    status_code: int = 200
    reason_phrase: str = "OK"
    text: str = '"pong"'
    content: bytes = b'"pong"'


class StubClient:
    def __init__(self, response: HttpResponse):
        self.response = response
        self.calls: list[tuple[str, dict[str, str], bytes]] = []

    async def post(self, url: str, *, headers: dict[str, str], content: bytes) -> HttpResponse:
        self.calls.append((url, headers, content))
        return self.response


class PrefixCodec(JournalValueCodec):
    def encode(self, buf: bytes) -> bytes:
        return b"codec:" + buf

    async def decode(self, buf: bytes) -> bytes:
        assert buf.startswith(b"codec:")
        return buf[len(b"codec:") :]


@pytest.mark.parametrize("blocked", [("httpx",), ("httpx", "httpx2")])
def test_import_without_http_packages(blocked: tuple[str, ...]):
    code = f"""
import sys
for name in {blocked!r}:
    sys.modules[name] = None
from restate.client import Client, create_client
from restate import HttpClient, HttpResponse
import restate
assert restate.create_client is create_client
assert restate.HttpClient is HttpClient
assert restate.HttpResponse is HttpResponse
for name in {blocked!r}:
    assert sys.modules[name] is None
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


async def test_supplied_client_request_and_codec():
    codec = PrefixCodec()
    http_client = StubClient(StubResponse(content=codec.encode(b'"pong"')))
    client = Client(http_client, journal_codec=codec)
    assert client.client is http_client
    headers = {"X-Test": "value"}

    result = await client.do_raw_call(
        service="greeter",
        handler="greet",
        input_param="ping",
        input_serde=JsonSerde[str](),
        output_serde=JsonSerde[str](),
        key="object",
        scope="tenant",
        headers=headers,
        idempotency_key="request-1",
        limit_key="customer/user",
    )

    assert result == "pong"
    assert http_client.calls == [
        (
            "restate/scope/tenant/call/greeter/object/greet",
            {"X-Test": "value", "Idempotency-Key": "request-1", "x-restate-limit-key": "customer/user"},
            b'codec:"ping"',
        )
    ]
    assert headers == {"X-Test": "value"}


async def test_supplied_client_send():
    http_client = StubClient(StubResponse(content=b'{"invocationId": "inv_123"}'))
    client = Client(http_client, journal_codec=PrefixCodec())
    result = await client.do_raw_call(
        service="greeter",
        handler="greet",
        input_param="ping",
        input_serde=JsonSerde[str](),
        output_serde=JsonSerde[dict](),
        send=True,
        send_delay=timedelta(seconds=2),
    )
    assert result == {"invocationId": "inv_123"}
    assert http_client.calls == [("greeter/greet/send?delay=2000", {}, b'codec:"ping"')]


@pytest.mark.parametrize("status_code", [400, 503])
async def test_http_error_fields(status_code: int):
    response = StubResponse(status_code, "custom reason", "exact body: ü\n")
    with pytest.raises(HttpError) as error:
        await Client(StubClient(response)).post("service", "handler", False, b"input")
    assert error.value.status_code == status_code
    assert error.value.message == response.reason_phrase
    assert error.value.body == response.text
    assert str(error.value) == f"HTTP {status_code}: custom reason"


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
@pytest.mark.parametrize("status_code", [200, 400, 503])
async def test_real_http_clients(backend: typing.Literal["httpx", "httpx2"], status_code: int):
    requests: list[tuple[str, dict[str, str], bytes]] = []

    def respond_httpx(request: httpx.Request) -> httpx.Response:
        requests.append((str(request.url), dict(request.headers), request.content))
        response = httpx.Response(status_code, content=b'"pong"')
        typed_response: HttpResponse = response
        assert typed_response.content == b'"pong"'
        return response

    def respond_httpx2(request: httpx2.Request) -> httpx2.Response:
        requests.append((str(request.url), dict(request.headers), request.content))
        response = httpx2.Response(status_code, content=b'"pong"')
        typed_response: HttpResponse = response
        assert typed_response.content == b'"pong"'
        return response

    # These assignments check both real client types against the protocol in Mypy and Pyright.
    context: typing.AsyncContextManager[HttpClient]
    if backend == "httpx":
        httpx_client: HttpClient = httpx.AsyncClient(
            base_url="http://ingress/", headers={"X-Default": "default"}, transport=httpx.MockTransport(respond_httpx)
        )
        assert isinstance(httpx_client, httpx.AsyncClient)
        context = httpx_client
    else:
        httpx2_client: HttpClient = httpx2.AsyncClient(
            base_url="http://ingress/", headers={"X-Default": "default"}, transport=httpx2.MockTransport(respond_httpx2)
        )
        assert isinstance(httpx2_client, httpx2.AsyncClient)
        context = httpx2_client

    async with context as supplied:
        client = Client(supplied)
        assert client.client is supplied
        if status_code >= 400:
            with pytest.raises(HttpError) as error:
                await client.post("service", "handler", False, b"input")
            assert error.value.status_code == status_code
            assert error.value.message == ("Bad Request" if status_code == 400 else "Service Unavailable")
            assert error.value.body == '"pong"'
        else:
            result = await client.do_raw_call(
                "service", "handler", "ping", JsonSerde[str](), JsonSerde[str](), headers={"X-Call": "call"}
            )
            assert result == "pong"

    url, headers, content = requests[0]
    assert url == "http://ingress/service/handler"
    assert headers["x-default"] == "default"
    if status_code == 200:
        assert headers["x-call"] == "call"
        assert content == b'"ping"'
    else:
        assert content == b"input"


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
@pytest.mark.parametrize("failure", ["timeout", "transport"])
async def test_transport_errors_propagate(backend: typing.Literal["httpx", "httpx2"], failure: str):
    httpx_error = httpx.ReadTimeout("timeout") if failure == "timeout" else httpx.ConnectError("connection")
    httpx2_error = httpx2.ReadTimeout("timeout") if failure == "timeout" else httpx2.ConnectError("connection")

    def fail_httpx(request: httpx.Request) -> httpx.Response:
        raise httpx_error

    def fail_httpx2(request: httpx2.Request) -> httpx2.Response:
        raise httpx2_error

    context: typing.AsyncContextManager[HttpClient]
    if backend == "httpx":
        context = httpx.AsyncClient(base_url="http://ingress/", transport=httpx.MockTransport(fail_httpx))
        expected: Exception = httpx_error
    else:
        context = httpx2.AsyncClient(base_url="http://ingress/", transport=httpx2.MockTransport(fail_httpx2))
        expected = httpx2_error
    async with context as supplied:
        with pytest.raises(type(expected)) as error:
            await Client(supplied).post("service", "handler", False, b"input")
        assert error.value is expected


class FactoryHttpClient(StubClient):
    def __init__(self, *, base_url: str, headers: dict | None, http2: bool):
        super().__init__(StubResponse())
        self.options = (base_url, headers, http2)
        self.entered = False
        self.closed = False
        self.exit_exception: BaseException | None = None

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.closed = True
        self.exit_exception = exc


@pytest.fixture
def factory_backends(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    constructors = {}
    for backend in ("httpx", "httpx2"):
        constructor = Mock(side_effect=FactoryHttpClient)
        module = ModuleType(backend)
        module.AsyncClient = constructor  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, backend, module)
        constructors[backend] = constructor
    return constructors


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
@pytest.mark.parametrize("available", [(), ("httpx",), ("httpx2",), ("httpx", "httpx2")])
async def test_factory_selection(
    backend: typing.Literal["httpx", "httpx2"],
    available: tuple[str, ...],
    factory_backends: dict[str, Mock],
    monkeypatch: pytest.MonkeyPatch,
):
    for name in ("httpx", "httpx2"):
        if name not in available:
            monkeypatch.setitem(sys.modules, name, None)  # type: ignore[arg-type]
    if backend not in available:
        with pytest.raises(ImportError) as error:
            async with create_client("http://ingress/", backend=backend):
                pytest.fail("Missing backend was accepted")
        extra = "client" if backend == "httpx" else "client-httpx2"
        assert f'python -m pip install "restate-sdk[{extra}]"' in str(error.value)
        assert isinstance(error.value.__cause__, ModuleNotFoundError)
        assert error.value.__cause__.name == backend
        assert not any(constructor.called for constructor in factory_backends.values())
        return

    headers = {"X-Default": "default"}
    codec = PrefixCodec()
    async with create_client("http://ingress/", headers, codec, backend=backend) as client:
        assert isinstance(client, Client)
        supplied = client.client
        assert isinstance(supplied, FactoryHttpClient)
        assert supplied.entered and not supplied.closed
        assert supplied.options == ("http://ingress/", headers, True)
        assert client.headers is headers
        assert client.journal_codec is codec
    assert supplied.closed
    factory_backends[backend].assert_called_once_with(base_url="http://ingress/", headers=headers, http2=True)
    other = "httpx2" if backend == "httpx" else "httpx"
    factory_backends[other].assert_not_called()


async def test_default_factory_and_positional_arguments(factory_backends: dict[str, Mock]):
    headers = {"X-Default": "value"}
    codec = PrefixCodec()
    async with restate.create_client("http://ingress/", headers, codec) as client:
        assert isinstance(client, Client)
        assert client.headers is headers
        assert client.journal_codec is codec
    factory_backends["httpx"].assert_called_once_with(base_url="http://ingress/", headers=headers, http2=True)
    factory_backends["httpx2"].assert_not_called()


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
async def test_factory_cleanup_on_error(backend: typing.Literal["httpx", "httpx2"], factory_backends: dict[str, Mock]):
    failure = RuntimeError("body failed")
    supplied: HttpClient | None = None
    with pytest.raises(RuntimeError) as error:
        async with create_client("http://ingress/", backend=backend) as client:
            assert isinstance(client, Client)
            supplied = client.client
            assert isinstance(supplied, FactoryHttpClient)
            raise failure
    assert error.value is failure
    assert isinstance(supplied, FactoryHttpClient)
    assert supplied.closed
    assert supplied.exit_exception is failure


async def test_invalid_backend_and_keyword_only(factory_backends: dict[str, Mock]):
    with pytest.raises(ValueError, match="Unsupported HTTP backend: 'unknown'"):
        async with create_client("http://ingress/", backend="unknown"):  # type: ignore[arg-type]
            pytest.fail("Invalid backend was accepted")
    with pytest.raises(TypeError):
        create_client("http://ingress/", None, None, "httpx2")  # type: ignore[misc]
    assert not any(constructor.called for constructor in factory_backends.values())


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
@pytest.mark.parametrize(
    "failure", [ModuleNotFoundError("missing dependency", name="dependency"), ImportError("broken")]
)
async def test_broken_backend_import_propagates(
    backend: typing.Literal["httpx", "httpx2"],
    failure: ImportError,
    factory_backends: dict[str, Mock],
    monkeypatch: pytest.MonkeyPatch,
):
    original_import = builtins.__import__

    def import_backend(name, *args, **kwargs):
        if name == backend:
            raise failure
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_backend)
    with pytest.raises(type(failure)) as error:
        async with create_client("http://ingress/", backend=backend):
            pytest.fail("Broken import was accepted")
    assert error.value is failure
    assert not any(constructor.called for constructor in factory_backends.values())
