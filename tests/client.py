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
import subprocess
import sys
import typing
from unittest.mock import ANY, MagicMock, Mock

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


def test_import_without_http_packages():
    code = """
import sys
sys.modules["httpx"] = sys.modules["httpx2"] = None
from restate.client import Client
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


async def test_supplied_client():
    http_client = StubClient(StubResponse())
    client = Client(http_client)
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
            b'"ping"',
        )
    ]
    assert headers == {"X-Test": "value"}


@pytest.mark.parametrize("status_code", [400, 503])
async def test_http_error_fields(status_code: int):
    response = StubResponse(status_code, "custom reason", "exact body: ü\n")
    with pytest.raises(HttpError) as error:
        await Client(StubClient(response)).post("service", "handler", False, b"input")
    assert error.value.status_code == status_code
    assert error.value.message == response.reason_phrase
    assert error.value.body == response.text
    assert str(error.value) == f"HTTP {status_code}: custom reason"


async def _check_real_client(http_client: HttpClient, respond: Mock):
    client = Client(http_client)
    assert client.client is http_client
    result = await client.do_raw_call(
        "service", "handler", "ping", JsonSerde[str](), JsonSerde[str](), headers={"X-Call": "call"}
    )
    assert result == "pong"
    (call,) = respond.call_args_list
    request = call.args[0]
    assert str(request.url) == "http://ingress/service/handler"
    assert request.headers["x-default"] == "default"
    assert request.headers["x-call"] == "call"
    assert request.content == b'"ping"'


async def test_httpx_client():
    respond = Mock(return_value=httpx.Response(200, content=b'"pong"'))
    async with httpx.AsyncClient(
        base_url="http://ingress/", headers={"X-Default": "default"}, transport=httpx.MockTransport(respond)
    ) as client:
        await _check_real_client(client, respond)


async def test_httpx2_client():
    respond = Mock(return_value=httpx2.Response(200, content=b'"pong"'))
    async with httpx2.AsyncClient(
        base_url="http://ingress/", headers={"X-Default": "default"}, transport=httpx2.MockTransport(respond)
    ) as client:
        await _check_real_client(client, respond)


@pytest.mark.parametrize(
    "http, failure",
    [
        (httpx, httpx.ReadTimeout("timeout")),
        (httpx, httpx.ConnectError("connection")),
        (httpx2, httpx2.ReadTimeout("timeout")),
        (httpx2, httpx2.ConnectError("connection")),
    ],
)
async def test_transport_errors_propagate(http, failure: Exception):
    transport = http.MockTransport(Mock(side_effect=failure))
    async with http.AsyncClient(base_url="http://ingress/", transport=transport) as client:
        with pytest.raises(type(failure)) as error:
            await Client(client).post("service", "handler", False, b"input")
    assert error.value is failure


@pytest.fixture
def factories(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    result = {}
    for http in (httpx, httpx2):
        client = MagicMock()
        client.__aenter__.return_value = client
        factory = Mock(return_value=client)
        monkeypatch.setattr(http, "AsyncClient", factory)
        result[http.__name__] = factory
    return result


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
@pytest.mark.parametrize("other_installed", [False, True])
async def test_factory_selects_backend(
    backend: typing.Literal["httpx", "httpx2"],
    other_installed: bool,
    factories: dict[str, Mock],
    monkeypatch: pytest.MonkeyPatch,
):
    other = "httpx2" if backend == "httpx" else "httpx"
    if not other_installed:
        monkeypatch.setitem(sys.modules, other, None)  # type: ignore[arg-type]
    headers = {"X-Default": "default"}
    codec = Mock(spec=JournalValueCodec)
    http_client = factories[backend].return_value
    async with create_client("http://ingress/", headers, codec, backend=backend) as client:
        assert isinstance(client, Client)
        assert client.client is http_client
        assert client.headers is headers
        assert client.journal_codec is codec
        http_client.__aexit__.assert_not_awaited()
    http_client.__aexit__.assert_awaited_once_with(None, None, None)
    factories[backend].assert_called_once_with(base_url="http://ingress/", headers=headers, http2=True)
    factories[other].assert_not_called()


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
@pytest.mark.parametrize("other_installed", [False, True])
async def test_missing_backend_does_not_fall_back(
    backend: typing.Literal["httpx", "httpx2"],
    other_installed: bool,
    factories: dict[str, Mock],
    monkeypatch: pytest.MonkeyPatch,
):
    other = "httpx2" if backend == "httpx" else "httpx"
    monkeypatch.setitem(sys.modules, backend, None)  # type: ignore[arg-type]
    if not other_installed:
        monkeypatch.setitem(sys.modules, other, None)  # type: ignore[arg-type]
    with pytest.raises(ImportError) as error:
        async with create_client("http://ingress/", backend=backend):
            pass
    extra = "client" if backend == "httpx" else "client-httpx2"
    assert f'python -m pip install "restate-sdk[{extra}]"' in str(error.value)
    factories[other].assert_not_called()


async def test_default_factory_and_positional_arguments(factories: dict[str, Mock]):
    headers = {"X-Default": "value"}
    codec = Mock(spec=JournalValueCodec)
    async with restate.create_client("http://ingress/", headers, codec):
        pass
    factories["httpx"].assert_called_once_with(base_url="http://ingress/", headers=headers, http2=True)
    factories["httpx2"].assert_not_called()


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
async def test_factory_cleanup_on_error(backend: typing.Literal["httpx", "httpx2"], factories: dict[str, Mock]):
    failure = RuntimeError("body failed")
    http_client = factories[backend].return_value
    with pytest.raises(RuntimeError) as error:
        async with create_client("http://ingress/", backend=backend):
            raise failure
    assert error.value is failure
    http_client.__aexit__.assert_awaited_once_with(RuntimeError, failure, ANY)


async def test_invalid_backend():
    with pytest.raises(ValueError, match="Unsupported HTTP backend: 'unknown'"):
        async with create_client("http://ingress/", backend="unknown"):  # type: ignore[arg-type]
            pass


def test_backend_is_keyword_only():
    with pytest.raises(TypeError):
        create_client("http://ingress/", None, None, "httpx2")  # type: ignore[misc]


@pytest.mark.parametrize("backend", ["httpx", "httpx2"])
@pytest.mark.parametrize(
    "failure", [ModuleNotFoundError("missing dependency", name="dependency"), ImportError("broken")]
)
async def test_broken_backend_import_propagates(
    backend: typing.Literal["httpx", "httpx2"],
    failure: ImportError,
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
            pass
    assert error.value is failure
