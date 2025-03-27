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
from restate import VirtualObject, ObjectSharedContext, ObjectContext
from restate import select
from restate.exceptions import TerminalError

virtual_object_command_interpreter = VirtualObject("VirtualObjectCommandInterpreter")

@virtual_object_command_interpreter.handler(name="getResults", kind="shared")
async def get_results(ctx: ObjectSharedContext) -> List[str]:
    return ctx.get("results") or []

@virtual_object_command_interpreter.handler(name="hasAwakeable", kind="shared")
async def has_awakeable(ctx: ObjectSharedContext, awk_key: str) -> bool:
    awk_id = ctx.get("awk-" + awk_key)
    if awk_id:
        return True
    return False

class CreateAwakeable(TypedDict):
    type: Literal["createAwakeable"]
    awakeableKey: str

class Sleep(TypedDict):
    type: Literal["sleep"]
    timeoutMillis: int

class RunThrowTerminalException(TypedDict):
    type: Literal["runThrowTerminalException"]
    reason: str

AwaitableCommand = Union[
    CreateAwakeable,
    Sleep,
    RunThrowTerminalException
]

class AwaitOne(TypedDict):
    type: Literal["awaitOne"]
    command: AwaitableCommand

class AwaitAnySuccessful(TypedDict):
    type: Literal["awaitAnySuccessful"]
    commands: List[AwaitableCommand]

class AwaitAny(TypedDict):
    type: Literal["awaitAny"]
    commands: List[AwaitableCommand]

class AwaitAwakeableOrTimeout(TypedDict):
    type: Literal["awaitAwakeableOrTimeout"]
    awakeableKey: str
    timeoutMillis: int

class ResolveAwakeable(TypedDict):
    type: Literal["resolveAwakeable"]
    awakeableKey: str
    value: str

class RejectAwakeable(TypedDict):
    type: Literal["rejectAwakeable"]
    awakeableKey: str
    reason: str

class GetEnvVariable(TypedDict):
    type: Literal["getEnvVariable"]
    envName: str

Command = Union[
    AwaitOne,
    AwaitAny,
    AwaitAnySuccessful,
    AwaitAwakeableOrTimeout,
    ResolveAwakeable,
    RejectAwakeable,
    GetEnvVariable
]

class InterpretRequest(TypedDict):
    commands: Iterable[Command]

@virtual_object_command_interpreter.handler(name="resolveAwakeable", kind="shared")
async def resolve_awakeable(ctx: ObjectSharedContext, req: ResolveAwakeable) -> bool:
    awk_id = ctx.get("awk-" + req.awakeableKey)
    if not awk_id:
        raise TerminalError(message="No awakeable is registered")
    ctx.resolve_awakeable(awk_id, req.value)

@virtual_object_command_interpreter.handler(name="rejectAwakeable", kind="shared")
async def reject_awakeable(ctx: ObjectSharedContext, req: RejectAwakeable) -> bool:
    awk_id = ctx.get("awk-" + req.awakeableKey)
    if not awk_id:
        raise TerminalError(message="No awakeable is registered")
    ctx.reject_awakeable(awk_id, req.reason)

@virtual_object_command_interpreter.handler(name="interpretCommands")
async def interpret_commands(ctx: ObjectContext, req: InterpretRequest):
    result = ""

    for cmd in req['commands']:
        if cmd['type'] == "awaitAwakeableOrTimeout":
            awk_id, awakeable = ctx.awakeable()
            ctx.get("awk-" + cmd.awakeableKey, awk_id)
            match await select(awakeable=awakeable, timeout=ctx.sleep(timedelta(milliseconds=cmd.timeoutMillis))):
                case ['awakeable', awk_res]:
                    result = awk_res
                case ['timeout', _]:
                    raise TerminalError(message="await-timeout", status_code=500)
        elif cmd['type'] == "resolveAwakeable":
            resolve_awakeable(ctx, cmd)
            result = ""
        elif cmd['type'] == "rejectAwakeable":
            reject_awakeable(ctx, cmd)
            result = ""
        elif cmd['type'] == "getEnvVariable":
            result = await ctx.run("get_env", lambda: os.environ.get(cmd['envName'], default=""))

        last_results = get_results(ctx)
        last_results.append(result)
        ctx.set("results", last_results)

    return result

