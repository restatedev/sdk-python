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
# pylint: disable=R0913,C0301,R0917
# pylint: disable=line-too-long
"""combines multiple futures into a single future"""

from typing import Any, List, Tuple
from restate.exceptions import TerminalError
from restate.context import RestateDurableFuture
from restate.server_context import ServerDurableFuture, ServerInvocationContext

async def gather(*futures: RestateDurableFuture[Any]) -> List[RestateDurableFuture[Any]]:
    """
    Blocks until all futures are completed.

    Returns a list of all futures.
    """
    async for _ in as_completed(*futures):
        pass
    return list(futures)

async def select(**kws: RestateDurableFuture[Any]) -> List[Any]:
    """
    Blocks until one of the futures is completed.

    Example:

    who, what = await select(car=f1, hotel=f2, flight=f3)
    if who == "car":
        print(what)
    elif who == "hotel":
        print(what)
    elif who == "flight":
        print(what)

    works the best with matching:

    match await select(result=ctx.promise("verify.payment"), timeout=ctx.sleep(timedelta(seconds=10))):
    case ['result', "approved"]:
        return { "success" : True }
    case ['result', "declined"]:
        raise TerminalError(message="Payment declined", status_code=401)
    case ['timeout', _]:
        raise TerminalError(message="Payment verification timed out", status_code=410)
     
    """
    if not kws:
        raise ValueError("At least one future must be passed.")
    reverse = { f: key for key, f in kws.items() }
    async for f in as_completed(*kws.values()):
        return [reverse[f], await f]
    assert False, "unreachable"

async def as_completed(*futures: RestateDurableFuture[Any]):
    """
    Returns an iterator that yields the futures as they are completed.
    
    example: 

    async for future in as_completed(f1, f2, f3):
        # do something with the completed future
        print(await future)  # prints the result of the future

    """
    remaining = list(futures)
    while remaining:
        completed, waiting = await wait_completed(*remaining)
        for f in completed:
            yield f
        remaining = waiting

async def wait_completed(*args: RestateDurableFuture[Any]) -> Tuple[List[RestateDurableFuture[Any]], List[RestateDurableFuture[Any]]]:
    """
    Blocks until at least one of the futures is completed.

    Returns a tuple of two lists: the first list contains the futures that are completed,
    the second list contains the futures that are not completed.
    """
    handles: List[int] = []
    context: ServerInvocationContext | None = None
    completed = []
    uncompleted = []
    futures = list(args)

    if not futures:
        return [], []
    for f in futures:
        if not isinstance(f, ServerDurableFuture):
            raise TerminalError("All futures must SDK created futures.")
        if context is None:
            context = f.context
        elif context is not f.context:
            raise TerminalError("All futures must be created by the same SDK context.")
        if f.is_completed():
            completed.append(f)
        else:
            handles.append(f.handle)
            uncompleted.append(f)

    if completed:
        # the user had passed some completed futures, so we can return them immediately
        return completed, uncompleted # type: ignore

    completed = []
    uncompleted = []
    assert context is not None
    await context.create_poll_or_cancel_coroutine(handles)

    for index, handle in enumerate(handles):
        future = futures[index]
        if context.vm.is_completed(handle):
            completed.append(future) # type: ignore
        else:
            uncompleted.append(future) # type: ignore
    return completed, uncompleted # type: ignore
