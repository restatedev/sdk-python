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
"""example.py"""
# pylint: disable=C0116
# pylint: disable=W0613

from datetime import timedelta
from restate import Service, Context
from restate.context import RestateDurableCallFuture, SendHandle
from typing import TypedDict, Optional, Iterable

proxy = Service("Proxy")


class ProxyRequest(TypedDict):
    serviceName: str
    virtualObjectKey: Optional[str]
    handlerName: str
    message: Iterable[int]
    delayMillis: Optional[int]
    idempotencyKey: Optional[str]
    scope: Optional[str]
    limitKey: Optional[str]


def do_call(ctx: Context, req: ProxyRequest) -> RestateDurableCallFuture[bytes]:
    """Issue the outgoing generic call described by req, forwarding scope/limitKey when set."""
    return ctx.generic_call(
        req["serviceName"],
        req["handlerName"],
        bytes(req["message"]),
        req.get("virtualObjectKey"),
        idempotency_key=req.get("idempotencyKey"),
        scope=req.get("scope"),
        limit_key=req.get("limitKey"),
    )


def do_send(ctx: Context, req: ProxyRequest) -> SendHandle:
    """Issue the outgoing generic send described by req, forwarding scope/limitKey when set."""
    send_delay = None
    delay_millis = req.get("delayMillis")
    if delay_millis is not None:
        send_delay = timedelta(milliseconds=delay_millis)
    return ctx.generic_send(
        req["serviceName"],
        req["handlerName"],
        bytes(req["message"]),
        req.get("virtualObjectKey"),
        send_delay=send_delay,
        idempotency_key=req.get("idempotencyKey"),
        scope=req.get("scope"),
        limit_key=req.get("limitKey"),
    )


@proxy.handler()
async def call(ctx: Context, req: ProxyRequest) -> Iterable[int]:
    response = await do_call(ctx, req)
    return list(response)


@proxy.handler(name="oneWayCall")
async def one_way_call(ctx: Context, req: ProxyRequest) -> str:
    handle = do_send(ctx, req)
    invocation_id = await handle.invocation_id()
    return invocation_id


class ManyCallRequest(TypedDict):
    proxyRequest: ProxyRequest
    oneWayCall: bool
    awaitAtTheEnd: bool


@proxy.handler(name="manyCalls")
async def many_calls(ctx: Context, requests: Iterable[ManyCallRequest]):
    to_await = []

    for req in requests:
        if req["oneWayCall"]:
            do_send(ctx, req["proxyRequest"])
        else:
            awaitable = do_call(ctx, req["proxyRequest"])
            if req["awaitAtTheEnd"]:
                to_await.append(awaitable)

    for awaitable in to_await:
        await awaitable
