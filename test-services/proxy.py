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

from restate import Service, Context
from typing import TypedDict, Optional, Iterable

proxy = Service("Proxy")


class ProxyRequest(TypedDict):
    serviceName: str
    virtualObjectKey: Optional[str]
    handlerName: str
    message: Iterable[int]


@proxy.handler()
async def call(ctx: Context, req: ProxyRequest) -> Iterable[int]:
    return list(await ctx.generic_call(
        req['serviceName'],
        req['handlerName'],
        bytes(req['message']),
        req['virtualObjectKey']))


@proxy.handler()
async def oneWayCall(ctx: Context, req: ProxyRequest):
    ctx.generic_send(
        req['serviceName'],
        req['handlerName'],
        bytes(req['message']),
        req['virtualObjectKey'])


class ManyCallRequest(TypedDict):
    proxyRequest: ProxyRequest
    oneWayCall: bool
    awaitAtTheEnd: bool

@proxy.handler()
async def manyCalls(ctx: Context, requests: Iterable[ManyCallRequest]):
    to_await = []

    for req in requests:
        if req['oneWayCall']:
            ctx.generic_send(
                req['proxyRequest']['serviceName'],
                req['proxyRequest']['handlerName'],
                bytes(req['proxyRequest']['message']),
                req['proxyRequest']['virtualObjectKey'])
        else:
            awaitable = ctx.generic_call(
                req['proxyRequest']['serviceName'],
                req['proxyRequest']['handlerName'],
                bytes(req['proxyRequest']['message']),
                req['proxyRequest']['virtualObjectKey'])
            if req['awaitAtTheEnd']:
                to_await.append(awaitable)

    for awaitable in to_await:
        await awaitable
