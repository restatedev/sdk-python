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
"""Verification test — class-based"""

from datetime import timedelta
import json
from typing import TypedDict
import typing
import random

from restate.cls import Service, handler, Context
from restate.exceptions import TerminalError
from restate.serde import JsonSerde

# Import the decorator-based VirtualObject for the dynamically-created layers
from restate.object import VirtualObject
from restate.context import ObjectContext, ObjectSharedContext

# suppress missing docstring
# pylint: disable=C0115
# pylint: disable=C0116
# pylint: disable=C0301
# pylint: disable=R0914, R0912, R0915, R0913

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


class ServiceInterpreterHelper(Service, name="ServiceInterpreterHelper"):

    @handler
    async def ping(self) -> None:
        pass

    @handler
    async def echo(self, parameters: str) -> str:
        return parameters

    @handler(name="echoLater")
    async def echo_later(self, parameter: dict[str, typing.Any]) -> str:
        await Context.sleep(timedelta(milliseconds=parameter["sleep"]))
        return parameter["parameter"]

    @handler(name="terminalFailure")
    async def terminal_failure(self) -> str:
        raise TerminalError("bye")

    @handler(name="incrementIndirectly")
    async def increment_indirectly(self, parameter) -> None:
        layer = parameter["layer"]
        key = parameter["key"]

        program = {
            "commands": [
                {
                    "kind": INCREMENT_STATE_COUNTER,
                },
            ],
        }

        program_bytes = json.dumps(program).encode("utf-8")
        Context.generic_send(f"ObjectInterpreterL{layer}", "interpret", program_bytes, key)

    @handler(name="resolveAwakeable")
    async def resolve_awakeable(self, aid: str) -> None:
        Context.resolve_awakeable(aid, "ok")

    @handler(name="rejectAwakeable")
    async def reject_awakeable(self, aid: str) -> None:
        Context.reject_awakeable(aid, "error")

    @handler(name="incrementViaAwakeableDance")
    async def increment_via_awakeable_dance(self, input: dict[str, typing.Any]) -> None:
        tx_promise_id = input["txPromiseId"]
        layer = input["interpreter"]["layer"]
        key = input["interpreter"]["key"]

        aid, promise = Context.awakeable()
        Context.resolve_awakeable(tx_promise_id, aid)
        await promise

        program = {
            "commands": [
                {
                    "kind": INCREMENT_STATE_COUNTER,
                },
            ],
        }

        program_bytes = json.dumps(program).encode("utf-8")
        Context.generic_send(f"ObjectInterpreterL{layer}", "interpret", program_bytes, key)


# Keep helper as a reference to the class for the __init__.py import
helper = ServiceInterpreterHelper


class SupportService:
    """Helper for making generic calls to ServiceInterpreterHelper."""
    def __init__(self) -> None:
        self.serde = JsonSerde[typing.Any]()

    async def call(self, method: str, arg: typing.Any) -> typing.Any:
        buffer = self.serde.serialize(arg)
        out_buffer = await Context.generic_call("ServiceInterpreterHelper", method, buffer)
        return self.serde.deserialize(out_buffer)

    def send(self, method: str, arg: typing.Any, delay: int | None = None) -> None:
        buffer = self.serde.serialize(arg)
        if delay is None:
            send_delay = None
        else:
            send_delay = timedelta(milliseconds=delay)
        Context.generic_send("ServiceInterpreterHelper", method, buffer, send_delay=send_delay)

    async def ping(self) -> None:
        return await self.call(method="ping", arg=None)

    async def echo(self, parameters: str) -> str:
        return await self.call(method="echo", arg=parameters)

    async def echo_later(self, parameter: str, sleep: int) -> str:
        arg = {"parameter": parameter, "sleep": sleep}
        return await self.call(method="echoLater", arg=arg)

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
        arg = {"interpreter": {"layer": layer, "key": key}, "txPromiseId": tx_promise_id}
        self.send("incrementViaAwakeableDance", arg)


class Command(TypedDict):
    kind: int
    key: int
    duration: int
    sleep: int
    index: int
    program: typing.Any  # avoid circular type


Program = dict[typing.Literal["commands"], typing.List[Command]]


async def interpreter(layer: int, program: Program) -> None:
    """Interprets a command and executes it."""
    service = SupportService()
    coros: dict[int, typing.Tuple[typing.Any, typing.Awaitable[typing.Any]]] = {}

    async def await_promise(index: int) -> None:
        if index not in coros:
            return

        expected, coro = coros[index]
        del coros[index]
        try:
            result = await coro
        except TerminalError:
            result = "rejected"

        if result != expected:
            raise TerminalError(f"Expected {expected} but got {result}")

    for i, command in enumerate(program["commands"]):
        command_type = command["kind"]
        if command_type == SET_STATE:
            Context.set(f"key-{command['key']}", f"value-{command['key']}")
        elif command_type == GET_STATE:
            await Context.get(f"key-{command['key']}")
        elif command_type == CLEAR_STATE:
            Context.clear(f"key-{command['key']}")
        elif command_type == INCREMENT_STATE_COUNTER:
            c = await Context.get("counter") or 0
            c += 1
            Context.set("counter", c)
        elif command_type == SLEEP:
            duration = timedelta(milliseconds=command["duration"])
            await Context.sleep(duration)
        elif command_type == CALL_SERVICE:
            expected = f"hello-{i}"
            coros[i] = (expected, service.echo(expected))
        elif command_type == INCREMENT_VIA_DELAYED_CALL:
            delay = command["duration"]
            await service.increment_indirectly(layer=layer, key=Context.key(), delay=delay)
        elif command_type == CALL_SLOW_SERVICE:
            expected = f"hello-{i}"
            coros[i] = (expected, service.echo_later(expected, command["sleep"]))
        elif command_type == SIDE_EFFECT:
            expected = f"hello-{i}"
            result = await Context.run_typed("sideEffect", lambda: expected)
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

            await Context.run_typed("throwingSideEffect", side_effect)
        elif command_type == INCREMENT_STATE_COUNTER_INDIRECTLY:
            await service.increment_indirectly(layer=layer, key=Context.key())
        elif command_type == AWAIT_PROMISE:
            index = command["index"]
            await await_promise(index)
        elif command_type == RESOLVE_AWAKEABLE:
            name, promise = Context.awakeable()
            coros[i] = ("ok", promise)
            service.resolve_awakeable(name)
        elif command_type == REJECT_AWAKEABLE:
            name, promise = Context.awakeable()
            coros[i] = ("rejected", promise)
            service.reject_awakeable(name)
        elif command_type == INCREMENT_STATE_COUNTER_VIA_AWAKEABLE:
            tx_promise_id, tx_promise = Context.awakeable()
            service.increment_via_awakeable_dance(layer=layer, key=Context.key(), tx_promise_id=tx_promise_id)
            their_promise_for_us_to_resolve: str = await tx_promise
            Context.resolve_awakeable(their_promise_for_us_to_resolve, "ok")
        elif command_type == CALL_NEXT_LAYER_OBJECT:
            next_layer = f"ObjectInterpreterL{layer + 1}"
            key = f"{command['key']}"
            program = command["program"]
            js_program = json.dumps(program)
            raw_js_program = js_program.encode("utf-8")
            promise = Context.generic_call(next_layer, "interpret", raw_js_program, key)
            coros[i] = (b"", promise)
        else:
            raise ValueError(f"Unknown command type: {command_type}")
        await await_promise(i)


# The layers use the original decorator-based API since they're dynamically created
def make_layer(i):
    layer = VirtualObject(f"ObjectInterpreterL{i}")

    @layer.handler()
    async def interpret(ctx: ObjectContext, program: Program) -> None:
        await interpreter(i, program)

    @layer.handler(kind="shared")
    async def counter(ctx: ObjectSharedContext) -> int:
        return await ctx.get("counter") or 0

    return layer


layer_0 = make_layer(0)
layer_1 = make_layer(1)
layer_2 = make_layer(2)
