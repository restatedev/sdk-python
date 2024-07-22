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
"""Verification test"""

from datetime import timedelta
import json
from typing import TypedDict
import typing
import random


from restate.context import ObjectContext, ObjectSharedContext
from restate.exceptions import TerminalError
from restate.object import VirtualObject
from restate.serde import JsonSerde
import restate

SET_STATE = 1
GET_STATE = 2
CLEAR_STATE = 3
INCREMENT_STATE_COUNTER = 4
INCREMENT_STATE_COUNTER_INDIRECTLY = 5
SLEEP = 6
CALL_SERVICE = 7
CALL_SLOW_SERVICE = 8
INCREMENT_VIA_DELAYED_CALL = 9
SIDE_EFFECT = 10
THROWING_SIDE_EFFECT = 11
SLOW_SIDE_EFFECT = 12
RECOVER_TERMINAL_CALL = 13
RECOVER_TERMINAL_MAYBE_UN_AWAITED = 14
AWAIT_PROMISE = 15
RESOLVE_AWAKEABLE = 16
REJECT_AWAKEABLE = 17
INCREMENT_STATE_COUNTER_VIA_AWAKEABLE = 18
CALL_NEXT_LAYER_OBJECT = 19

# suppress missing docstring
# pylint: disable=C0115
# pylint: disable=C0116
# pylint: disable=C0301
# pylint: disable=R0914, R0912, R0915, R0913

class SupportService:

    def __init__(self, ctx: ObjectContext) -> None:
        self.ctx = ctx
        self.serde = JsonSerde[typing.Any]()

    async def call(self, method: str, arg: typing.Any) -> typing.Any:
        buffer = self.serde.serialize(arg)
        out_buffer = await self.ctx.generic_call("ServiceInterpreterHelper", method, buffer)
        return self.serde.deserialize(out_buffer)

    def send(self, method: str, arg: typing.Any, delay: int | None = None) -> None:
        buffer = self.serde.serialize(arg)
        if delay is None:
            send_delay = None
        else:
            send_delay = timedelta(milliseconds=delay)
        self.ctx.generic_send("ServiceInterpreterHelper", method, buffer, send_delay=send_delay)

    async def ping(self) -> None:
        return await self.call(method="ping", arg=None)

    async def echo(self, parameters: str) -> str:
        return await self.call(method="echo", arg=parameters)

    async def echo_later(self, parameter: str, sleep: int) -> str:
        arg = {"parameter": parameter, "sleep": sleep}
        return await self.call(method="echo_later", arg=arg)

    async def terminal_failure(self) -> str:
        return await self.call(method="terminalFailure", arg=None)

    async def increment_indirectly(self, layer: int, key: str, delay: typing.Optional[int] = None) -> None:
        arg = {"layer": layer, "key": key}
        self.send(method="incrementIndirectly", arg=arg, delay=delay)

    def resolve_awakeable(self, aid: str) -> None:
        self.send("resolveAwakeable", aid)

    def reject_awakeable(self, aid: str) -> None:
        self.send("rejectAwakeable", aid)

    def increment_via_awakeable_dance(self, layer: int, key: str, tx_promise_id: str) -> None:
        arg = { "interpreter" : { "layer": layer, "key": key} , "txPromiseId": tx_promise_id }
        self.send("incrementViaAwakeableDance", arg)


class Command(TypedDict):
    kind: int
    key: int
    duration: int
    sleep: int
    index: int
    program: typing.Any # avoid circular type


Program = dict[typing.Literal['commands'],
               typing.List[Command]]


async def interpreter(layer: int,
                      ctx: ObjectContext,
                      program: Program) -> None:
    """Interprets a command and executes it."""
    service = SupportService(ctx)
    coros: dict[int,
                typing.Tuple[typing.Any, typing.Awaitable[typing.Any]]] = {}
    for i, command in enumerate(program['commands']):
        print(f"{ctx.request().id} {ctx.key()} {id(ctx)} COMMAND {i} {command}", flush=True)
        command_type = command['kind']
        if command_type == SET_STATE:
            ctx.set(f"key-{command['key']}", f"value-{command['key']}")
        elif command_type == GET_STATE:
            await ctx.get(f"key-{command['key']}")
        elif command_type == CLEAR_STATE:
            ctx.clear(f"key-{command['key']}")
        elif command_type == INCREMENT_STATE_COUNTER:
            c = await ctx.get("counter") or 0
            c += 1
            ctx.set("counter", c)
        elif command_type == SLEEP:
            duration = timedelta(milliseconds=command['duration'])
            await ctx.sleep(duration)
        elif command_type == CALL_SERVICE:
            expected = f"hello-{i}"
            coros[i] = (expected, service.echo(expected))
        elif command_type == INCREMENT_VIA_DELAYED_CALL:
            delay = command['duration']
            await service.increment_indirectly(layer=layer, key=ctx.key(), delay=delay)
        elif command_type == CALL_SLOW_SERVICE:
            expected = f"hello-{i}"
            coros[i] = (expected, service.echo_later(expected, command['sleep']))
        elif command_type == SIDE_EFFECT:
            expected = f"hello-{i}"
            result = await ctx.run("sideEffect",  lambda : expected) # pylint: disable=W0640
            if result != expected:
                raise TerminalError(f"Expected {expected} but got {result}")
        elif command_type == SLOW_SIDE_EFFECT:
            pass
        elif command_type == RECOVER_TERMINAL_CALL:
            try:
                await service.terminal_failure()
            except TerminalError:
                pass
            else:
                raise TerminalError("Expected terminal error")
        elif command_type == RECOVER_TERMINAL_MAYBE_UN_AWAITED:
            pass
        elif command_type == THROWING_SIDE_EFFECT:
            async def side_effect():
                if bool(random.getrandbits(1)):
                    raise ValueError("Random error")

            await ctx.run("throwingSideEffect", side_effect)
        elif command_type == INCREMENT_STATE_COUNTER_INDIRECTLY:
            await service.increment_indirectly(layer=layer, key=ctx.key())
        elif command_type == AWAIT_PROMISE:
            index = command['index']
            if index not in coros:
                continue

            expected, coro = coros[index]
            del coros[index]
            print(f"AWAITING {index} {coro}", flush=True)
            result = await coro
            print (f"AWAITED {index} {result}", flush=True)
            print(f"EXPECTED {result} {expected}", flush=True)
            if result != expected:
                raise TerminalError(f"Expected {expected} but got {result}")
        elif command_type == RESOLVE_AWAKEABLE:
            name, promise = ctx.awakeable()
            coros[i] = ("ok", promise)
            service.resolve_awakeable(name)
        elif command_type == REJECT_AWAKEABLE:
            name, promise = ctx.awakeable()
            coros[i] = ("rejected", promise)
            service.reject_awakeable(name)
        elif command_type == INCREMENT_STATE_COUNTER_VIA_AWAKEABLE:
            tx_promise_id, tx_promise = ctx.awakeable()
            service.increment_via_awakeable_dance(layer=layer, key=ctx.key(), tx_promise_id=tx_promise_id)
            their_promise_for_us_to_resolve: str = await tx_promise
            ctx.resolve_awakeable(their_promise_for_us_to_resolve, "ok")
        elif command_type == CALL_NEXT_LAYER_OBJECT:
            next_layer = f"ObjectInterpreterL{layer + 1}"
            key = f"{command['key']}"
            program = command['program']
            js_program = json.dumps(program)
            raw_js_program = js_program.encode('utf-8')
            promise = ctx.generic_call(next_layer, "interpret", raw_js_program, key)
            print(f"Storing coro {i} {promise}", flush=True)
            coros[i] = (b'', promise)
        else:
            raise ValueError(f"Unknown command type: {command_type}")
        print(f"{ctx.request().id} {ctx.key()} {id(ctx)} DONE command {i}", flush=True)
    print("DONE " + str(len(program['commands'])) , flush=True)

layer_0 = VirtualObject("ObjectInterpreterL0")

@layer_0.handler()
async def interpret(ctx: ObjectContext, program: Program) -> None:
    await interpreter(0, ctx, program)

@layer_0.handler(kind="shared")
async def counter(ctx: ObjectSharedContext) -> int:
    return await ctx.get("counter") or 0


app = restate.app(services=[layer_0])
