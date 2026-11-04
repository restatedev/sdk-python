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
import warnings

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

    .. deprecated:: 1.4.0
        Pass ``explicit_recovery=True`` to the handler decorator, or to the service, instead.

    NOTE: This API is in preview and might change without notice.
    """
    warnings.warn(
        "restate._preview.explicit_recovery is deprecated: pass explicit_recovery=True to the handler decorator, "
        "or to the service, instead",
        DeprecationWarning,
        stacklevel=2,
    )
    if isinstance(target, (Service, VirtualObject, Workflow)):
        target.explicit_recovery = True
    else:
        handler_from_callable(target).explicit_recovery = True  # type: ignore
    return target


def cancellation(ctx: Context) -> RestateDurableFuture[None]:
    """
    Return a future that completes when Restate cancels the invocation of `ctx`.

    Await it together with the handler's other work, for example with `restate.select`::

        @greeter.handler(explicit_cancellation=True)
        async def greet(ctx: Context, name: str) -> str:
            work = ctx.service_call(slow_greeting, arg=name)
            done, _ = await restate.select(work=work, cancelled=cancellation(ctx))
            if done == "cancelled":
                await cancel_previous_calls(ctx)
                raise TerminalError("cancelled", status_code=409)
            ...

    The handler must run with ``explicit_cancellation=True`` (on the handler, or on its service): then
    Restate cancelling the invocation neither fails the handler's awaits nor cancels its calls.

    NOTE: This API is in preview and might change without notice.
    """
    return _server_context(ctx, "cancellation").cancellation()


async def cancel_previous_calls(ctx: Context) -> typing.List[str]:
    """
    Cancel the calls that this attempt of the handler made and that have not completed yet.

    This is what Restate does on its own for a handler without explicit cancellation. Returns the invocation
    ids of the cancelled calls. The handler must run with ``explicit_cancellation=True``.

    NOTE: This API is in preview and might change without notice.
    """
    return await _server_context(ctx, "cancel_previous_calls").cancel_previous_calls()


def _server_context(ctx: Context, operation: str) -> ServerInvocationContext:
    if not isinstance(ctx, ServerInvocationContext):
        raise TerminalError(f"{operation}() takes the context a handler is invoked with")
    return ctx
