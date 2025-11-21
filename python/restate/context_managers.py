#
#  Copyright (c) 2023-2025 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""
contextvar utility for async context managers.
"""

import contextvars
from contextlib import asynccontextmanager
from typing import (
    Any,
    AsyncContextManager,
    AsyncGenerator,
    Callable,
    Generic,
    ParamSpec,
    TypeVar,
)

P = ParamSpec("P")
T = TypeVar("T")


class contextvar(Generic[P, T]):
    """
    A type-safe decorator for asynccontextmanager functions that captures the yielded value in a ContextVar.
    This is useful when integrating with frameworks that only support None yielded values from context managers.

    Example usage:
    ```python
    @contextvar
    @asynccontextmanager
    async def my_resource() -> AsyncIterator[str]:
        yield "hi"

    async def usage_example():
        async with my_resource():
            print(my_resource.value)  # prints "hi"
    ```


    """

    def __init__(self, func: Callable[P, AsyncContextManager[T]]):
        self.func = func
        self._value_var: contextvars.ContextVar[T | None] = contextvars.ContextVar("value")

    @property
    def value(self) -> T:
        """Return the value yielded by the wrapped context manager."""
        val = self._value_var.get()
        if val is None:
            raise LookupError("Context manager value accessed outside of context manager scope (has not been entered yet)")
        return val

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> AsyncContextManager[None]:
        @asynccontextmanager
        async def wrapper() -> AsyncGenerator[None, Any]:
            async with self.func(*args, **kwargs) as value:
                token = self._value_var.set(value)
                try:
                    yield  # we make it yield None, as the value is accessible via .value()
                finally:
                    self._value_var.reset(token)

        return wrapper()
