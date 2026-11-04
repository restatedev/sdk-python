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
"""Preview apis: they might change or be removed without notice."""

import typing

from .context import Context, RestateDurableFuture
from .exceptions import TerminalError
from .handler import handler_from_callable
from .server_context import ServerInvocationContext
from .service import Service
from .object import VirtualObject
from .workflow import Workflow

T = typing.TypeVar("T")


def explicit_recovery(target: T) -> T:
    """
    Run the handlers with explicit recovery: a retried or resumed invocation doesn't replay the journal of its
    earlier attempts, but runs the handler again from the start, executing every context operation anew,
    e.g. reading the current state.

    The handler takes care of recovering from the effects of the earlier attempts, which might repeat (at least once):

    * Each attempt's calls and sends are new requests. To reach the invocation an earlier attempt started,
      give it an idempotency key that is the same on every attempt, such as one made from `ctx.request().id`.
    * `ctx.run` executes its action again on every attempt.
    * Awakeables are not supported.

    Apply it to a service, virtual object or workflow to enable it for all its handlers,
    or as a decorator on top of a handler to enable it for that handler only.

    Example:
        counter = explicit_recovery(VirtualObject("counter"))

        greeter = Service("greeter")

        @explicit_recovery
        @greeter.handler()
        async def greet(ctx: Context, name: str) -> str:
            ...

    NOTE: This API is in preview and might change without notice.
    """
    if isinstance(target, (Service, VirtualObject, Workflow)):
        target.explicit_recovery = True
    else:
        handler_from_callable(target).explicit_recovery = True  # type: ignore
    return target


def explicit_cancellation(target: T) -> T:
    """
    Run the handlers with explicit cancellation: when Restate cancels an invocation, no await fails, and the calls
    it made are not cancelled. The handler learns of it with `cancellation(ctx)`, and decides what to stop: such as
    to cancel the calls it made, with `cancel_previous_calls(ctx)`, and to end with a TerminalError.

    Apply it to a service, virtual object or workflow to enable it for all its handlers,
    or as a decorator on top of a handler to enable it for that handler only.

    Example:
        @explicit_cancellation
        @greeter.handler()
        async def greet(ctx: Context, name: str) -> str:
            work = ctx.service_call(slow, arg=name)
            done = await restate.select(work=work, cancelled=cancellation(ctx))
            if done[0] == "cancelled":
                await cancel_previous_calls(ctx)
                raise TerminalError("cancelled", 409)
            ...

    NOTE: This API is in preview and might change without notice.
    """
    if isinstance(target, (Service, VirtualObject, Workflow)):
        target.explicit_cancellation = True
    else:
        handler_from_callable(target).explicit_cancellation = True  # type: ignore
    return target


def cancellation(ctx: Context) -> RestateDurableFuture[None]:
    """
    Completes when Restate cancels the invocation of `ctx`, a handler's with explicit cancellation (see
    `explicit_cancellation`). Await it together with the handler's other work, such as with `restate.select`.

    NOTE: This API is in preview and might change without notice.
    """
    return _server_context(ctx, "cancellation").cancellation()


async def cancel_previous_calls(ctx: Context) -> typing.List[str]:
    """
    Cancel the calls that the attempt of `ctx`, a handler's with explicit cancellation, made so far and that have not
    completed, as a cancelled invocation's are without explicit cancellation. Returns their invocation ids.

    NOTE: This API is in preview and might change without notice.
    """
    return await _server_context(ctx, "cancel_previous_calls").cancel_previous_calls()


def _server_context(ctx: Context, operation: str) -> ServerInvocationContext:
    if not isinstance(ctx, ServerInvocationContext):
        raise TerminalError(f"{operation} takes the context of a handler")
    return ctx
