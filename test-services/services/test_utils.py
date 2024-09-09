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

import os
from datetime import timedelta
from typing import (Dict, Iterable, List, Union, TypedDict, Literal, Any)
from restate import Service, Context

from . import list_object
from . import awakeable_holder

test_utils = Service("TestUtilsService")

@test_utils.handler()
async def echo(context: Context, input: str) -> str:
    return input

@test_utils.handler(name="uppercaseEcho")
async def uppercase_echo(context: Context, input: str) -> str:
    return input.upper()

@test_utils.handler(name="echoHeaders")
async def echo_headers(context: Context) -> Dict[str, str]:
    return context.request().headers

@test_utils.handler(name="createAwakeableAndAwaitIt")
async def create_awakeable_and_await_it(context: Context, req: Dict[str, Any]) -> Dict[str, Any]:
    name, awakeable = context.awakeable()

    await context.object_call(awakeable_holder.hold, key=req["awakeableKey"], arg=name)

    if "awaitTimeout" not in req:
        return {"type": "result", "value": await awakeable}

    timeout = context.sleep(timedelta(milliseconds=int(req["awaitTimeout"])))
    raise NotImplementedError()

@test_utils.handler(name="sleepConcurrently")
async def sleep_concurrently(context: Context, millis_duration: List[int]) -> None:
    timers = [context.sleep(timedelta(milliseconds=duration)) for duration in millis_duration]

    for timer in timers:
        await timer


@test_utils.handler(name="countExecutedSideEffects")
async def count_executed_side_effects(context: Context, increments: int) -> int:
    invoked_side_effects = 0

    def effect():
        nonlocal invoked_side_effects
        invoked_side_effects += 1

    for _ in range(increments):
        await context.run("count", effect)

    return invoked_side_effects

@test_utils.handler(name="getEnvVariable")
async def get_env_variable(context: Context, env_name: str) -> str:
    return os.environ.get(env_name, default="")

class CreateAwakeableAndAwaitIt(TypedDict):
    type: Literal["createAwakeableAndAwaitIt"]
    awakeableKey: str

class GetEnvVariable(TypedDict):
    type: Literal["getEnvVariable"]
    envName: str

Command = Union[
    CreateAwakeableAndAwaitIt,
    GetEnvVariable
]

class InterpretRequest(TypedDict):
    listName: str
    commands: Iterable[Command]

@test_utils.handler(name="interpretCommands")
async def interpret_commands(context: Context, req: InterpretRequest):
    for cmd in req['commands']:
        if cmd['type'] == "createAwakeableAndAwaitIt":
            name, awakeable = context.awakeable()
            context.object_send(awakeable_holder.hold, key=cmd["awakeableKey"], arg=name)
            result = await awakeable
            context.object_send(list_object.append, key=req['listName'], arg=result)
        elif cmd['type'] == "getEnvVariable":
            context.object_send(list_object.append, key=req['listName'], arg=os.environ.get(cmd['envName'], default=""))
