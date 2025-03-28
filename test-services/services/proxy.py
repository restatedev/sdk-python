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
from typing import TypedDict, Optional, Iterable

proxy = Service("Proxy")


class ProxyRequest(TypedDict):
    serviceName: str
    virtualObjectKey: Optional[str]
    handlerName: str
    message: Iterable[int]
    delayMillis: Optional[int]
    idempotencyKey: Optional[str]


@proxy.handler()
async def call(ctx: Context, req: ProxyRequest) -> Iterable[int]:
    response = await ctx.generic_call(
        req['serviceName'],
        req['handlerName'],
        bytes(req['message']),
        req.get('virtualObjectKey'),
        req.get('idempotencyKey'))
    return list(response)


@proxy.handler(name="oneWayCall")
async def one_way_call(ctx: Context, req: ProxyRequest) -> str:
    send_delay = None
    if req.get('delayMillis'):
        send_delay = timedelta(milliseconds=req['delayMillis'])
    handle = ctx.generic_send(
        req['serviceName'],
        req['handlerName'],
        bytes(req['message']),
        req.get('virtualObjectKey'),
        send_delay=send_delay,
        idempotency_key=req.get('idempotencyKey')
    )
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
        if req['oneWayCall']:
            send_delay = None
            if req['proxyRequest'].get('delayMillis'):
                send_delay = timedelta(milliseconds=req['proxyRequest']['delayMillis'])
            ctx.generic_send(
                req['proxyRequest']['serviceName'],
                req['proxyRequest']['handlerName'],
                bytes(req['proxyRequest']['message']),
                req['proxyRequest'].get('virtualObjectKey'),
                send_delay=send_delay,
                idempotency_key=req['proxyRequest'].get('idempotencyKey')
            )
        else:
            awaitable = ctx.generic_call(
                req['proxyRequest']['serviceName'],
                req['proxyRequest']['handlerName'],
                bytes(req['proxyRequest']['message']),
                req['proxyRequest'].get('virtualObjectKey'),
                idempotency_key=req['proxyRequest'].get('idempotencyKey'))
            if req['awaitAtTheEnd']:
                to_await.append(awaitable)

    for awaitable in to_await:
        await awaitable
