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

class AttemptShutDownException(Exception):
    """Restate shut the attempt down: a new attempt will run the handler from the start."""

class PyInput:
    invocation_id: str
    random_seed: int
    key: str
    headers: list[Any]
    input: bytes
    scope: str | None
    limit_key: str | None
    idempotency_key: str | None

class PyExplicitRecoveryVM:
    """The VM of a session object's attempt (``src/explicit_recovery.rs``)."""

    def __init__(self, headers: Sequence[tuple[str, str]], bidirectional: bool) -> None: ...
    def set_scheduler(self, scheduler: Callable[[], None]) -> None: ...
    def get_response_head(self) -> Any: ...
    def notify_input(self, buffer: bytes) -> None: ...
    def notify_input_closed(self) -> None: ...
    def take_output(self) -> bytes: ...
    def is_ready_to_execute(self) -> bool: ...
    def input(self) -> PyInput: ...
    def take_notification(self) -> tuple[int, Any] | None: ...
    def get_state(self, key: str, name: str | None = None) -> int: ...
    def get_state_keys(self, name: str | None = None) -> int: ...
    def call(
        self,
        service: str,
        handler: str,
        buffer: bytes,
        key: str | None = None,
        idempotency_key: str | None = None,
        headers: Sequence[Any] | None = None,
        name: str | None = None,
    ) -> Any: ...
    def attach_invocation(self, invocation_id: str, name: str | None = None) -> int: ...
    def set_state(self, key: str, buffer: bytes, name: str | None = None, ack: bool = False) -> int: ...
    def clear_state(self, key: str, name: str | None = None, ack: bool = False) -> int: ...
    def clear_all_state(self, name: str | None = None, ack: bool = False) -> int: ...
    def cancel_invocation(self, invocation_id: str, name: str | None = None, ack: bool = False) -> int: ...
    def send(
        self,
        service: str,
        handler: str,
        buffer: bytes,
        key: str | None = None,
        delay: int | None = None,
        idempotency_key: str | None = None,
        headers: Sequence[Any] | None = None,
        name: str | None = None,
    ) -> int: ...
    def start_trace(self, name: str, parent: int | None = None) -> int: ...
    def end_trace_success(self, trace: int, buffer: bytes) -> None: ...
    def end_trace_failure(self, trace: int, failure: Any) -> None: ...
    def heartbeat(self) -> None: ...
    def end_success(self, buffer: bytes) -> None: ...
    def end_failure(self, failure: Any) -> None: ...
    def notify_error(self, error: str, stacktrace: str | None = None, delay_override_ms: int | None = None) -> None: ...
    def is_cancelled(self) -> bool: ...
    def state(self) -> str: ...

class PyVM:
    """The invocation's VM (``restate.vm.VMWrapper`` wraps it)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None: ...
    def __getattr__(self, name: str) -> Any: ...

# Declared, as names ``restate.connection`` re-exports must be (still untyped).

class ConnectError(Exception):
    """Connecting to Restate Cloud failed for good (``src/tunnel``)."""

class TunnelInfo:
    """A Restate Cloud connection's details."""

    def __getattr__(self, name: str) -> Any: ...

# --- Sessions (src/sessions.rs) -----------------------------------------------------------------

SQL_GET: int
SQL_SET: int
SQL_CLEAR: int
SQL_SYNC: int
SQL_KEYS: int
SQL_COMMIT: int

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
    vm: PyExplicitRecoveryVM | None = None,
    name: str | None = None,
) -> _Answer[PySqlDatabase]: ...
