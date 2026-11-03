# Copyright (c) 2026 - Restate Software, Inc., Restate GmbH
# Released under the MIT license; see LICENSE.

from datetime import timedelta

import httpx
import pytest
import restate
from restate.client import Client


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("key", "encoded"),
    [
        ("tenant/a?b#c%25ü", "tenant%2Fa%3Fb%23c%2525%C3%BC"),
        ("a b+c", "a%20b%2Bc"),
        ("safe-key_01.2", "safe-key_01.2"),
    ],
)
@pytest.mark.parametrize("scope", [None, "team/one?two#three"])
@pytest.mark.parametrize("send", [False, True])
async def test_generic_object_key_is_one_url_segment(key, encoded, scope, send):
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, content=b'{"invocationId":"inv_123"}' if send else b"ok")

    async with httpx.AsyncClient(
        base_url="https://restate.example/ingress/", transport=httpx.MockTransport(handle)
    ) as http:
        client = Client(http)
        if send:
            result = await client.generic_send(
                "Counter",
                "add",
                b"payload",
                key=key,
                scope=scope,
                send_delay=timedelta(milliseconds=1250),
                idempotency_key="unique",
            )
            assert result.invocation_id == "inv_123"
        else:
            assert (
                await client.generic_call("Counter", "add", b"payload", key=key, scope=scope, idempotency_key="unique")
                == b"ok"
            )

    if scope is not None:
        verb = "send" if send else "call"
        expected = f"/ingress/restate/scope/team%2Fone%3Ftwo%23three/{verb}/Counter/{encoded}/add"
    else:
        expected = f"/ingress/Counter/{encoded}/add" + ("/send" if send else "")
    if send:
        expected += "?delay=1250"
    assert len(requests) == 1
    assert requests[0].url.raw_path == expected.encode()
    assert not requests[0].url.fragment
    assert requests[0].content == b"payload"
    assert requests[0].headers["Idempotency-Key"] == "unique"


counter = restate.VirtualObject("Counter")


@counter.handler()
async def increment(ctx: restate.ObjectContext, arg: str) -> str:
    return arg


workflow = restate.Workflow("Workflow")


@workflow.main()
async def run(ctx: restate.WorkflowContext, arg: str) -> str:
    return arg


@pytest.mark.anyio
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("method", ["object_call", "object_send", "workflow_call", "workflow_send"])
async def test_typed_object_and_workflow_clients_escape_keys(method, scoped):
    requests = []
    send = method.endswith("send")

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"invocationId": "inv_123"} if send else "ok")

    async with httpx.AsyncClient(base_url="https://restate.example", transport=httpx.MockTransport(handle)) as http:
        client = Client(http)
        target = client.scope("team/one") if scoped else client
        handler = run if method.startswith("workflow") else increment
        result = await getattr(target, method)(handler, key="object/a?b#c", arg="payload")
        if not send:
            assert result == "ok"

    service, name = ("Workflow", "run") if method.startswith("workflow") else ("Counter", "increment")
    if scoped:
        expected = f"/restate/scope/team%2Fone/{'send' if send else 'call'}/{service}/object%2Fa%3Fb%23c/{name}"
    else:
        expected = f"/{service}/object%2Fa%3Fb%23c/{name}" + ("/send" if send else "")
    assert len(requests) == 1
    assert requests[0].url.raw_path == expected.encode()
    assert requests[0].url.query == b""
    assert requests[0].content == b'"payload"'


@pytest.mark.anyio
@pytest.mark.parametrize("scope", [None, "unchanged"])
@pytest.mark.parametrize("send", [False, True])
async def test_service_routes_without_keys_are_unchanged(scope, send):
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, content=b'{"invocationId":"inv_123"}' if send else b"ok")

    async with httpx.AsyncClient(base_url="https://restate.example", transport=httpx.MockTransport(handle)) as http:
        client = Client(http)
        if send:
            await client.generic_send("Service", "handler", b"payload", scope=scope, limit_key="limit/one")
        else:
            await client.generic_call("Service", "handler", b"payload", scope=scope, limit_key="limit/one")

    if scope is None:
        expected = "/Service/handler" + ("/send" if send else "")
    else:
        expected = f"/restate/scope/unchanged/{'send' if send else 'call'}/Service/handler"
    assert requests[0].url.raw_path == expected.encode()
    assert requests[0].headers["x-restate-limit-key"] == "limit/one"
