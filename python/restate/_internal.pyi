#
#  Copyright (c) 2023-2026 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""
The native extension (``src/``), as far as it is typed: the sessions'
database (``src/sessions.rs``) is; the VM's bindings (``src/lib.rs``), which
``restate.vm`` wraps, are left to ``__getattr__``, as untyped as without this
stub.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, TypeVar, Union

_T = TypeVar("_T")

_Answer = Union[_T, Awaitable[_T]]
"""A call's answer: its value, if it is ready at once, or an asyncio future of it."""

_SqlValue = Union[None, int, float, str, bytes]

def __getattr__(name: str) -> Any: ...

class PyVM:
    """The invocation's VM (``restate.vm.VMWrapper`` wraps it)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None: ...
    def __getattr__(self, name: str) -> Any: ...

# --- Sessions (src/sessions.rs) -----------------------------------------------------------------

SQL_GET: int
SQL_SET: int
SQL_CLEAR: int
SQL_SYNC: int
SQL_KEYS: int
SQL_WAIT: int

class SqlRefusedException(Exception):
    """A failure of the database that a retry would repeat (shared-core's ``sql::Refused``)."""

class PySqlDatabase:
    """One session of a namespace's database."""

    def query_columns(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> _Answer[tuple[list[str], list[list[_SqlValue]]]]: ...
    def begin(self) -> _Answer[None]: ...
    def commit(self, records: Mapping[str, bytes] | None = None) -> _Answer[None]: ...
    def rollback(self) -> _Answer[None]: ...
    def is_usable(self) -> bool: ...
    def record(self, name: str) -> _Answer[bytes]: ...
    def save(self) -> _Answer[None]: ...
    def close(self) -> _Answer[None]: ...

class SqlReply:
    """The callable ``dispatch`` answers a request with: ``reply(value)`` or ``reply(None, error)``."""

    def __call__(self, value: object | None, error: str | None = None) -> None: ...

def sql_open(
    namespace: str,
    dispatch: Callable[[int, Any, bytes | None, SqlReply | None], None],
    lists_keys: bool = False,
    reader: bool = False,
    vm: PyVM | None = None,
    name: str | None = None,
) -> _Answer[PySqlDatabase]: ...
