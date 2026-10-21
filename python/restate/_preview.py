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

from .handler import handler_from_callable
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
