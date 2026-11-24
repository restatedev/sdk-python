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
from typing import Any, Literal, TypeVar, Union

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
    attempt: int | None
    """The attempt's number, from 1, on the explicit recovery protocol; ``None`` on the durable execution one."""

_Record = Literal["default", "none", "summary", "full"] | None
_Scope = Literal["object", "invocation"]

def explicit_recovery_protocol_range() -> tuple[int, int]:
    """The versions of the explicit recovery protocol the VM speaks: ``(min, max)``."""

class PyExplicitRecoveryVM:
    """The VM of a session object's attempt, on the explicit recovery protocol (``src/explicit_recovery.rs``).

    Every action takes the keywords ``name``, ``parent_span`` and ``record``. An action whose result is optional
    takes ``want_result``, and returns ``None`` without it.
    """

    def __init__(self, headers: Sequence[tuple[str, str]]) -> None:
        """Raises ``VMException(message, code)``: answer the request with ``code`` (415), and no body."""
    def set_scheduler(self, scheduler: Callable[[], None]) -> None: ...
    def get_response_head(self) -> Any: ...
    def notify_input(self, buffer: bytes) -> None: ...
    def notify_input_closed(self) -> None: ...
    def take_output(self) -> bytes: ...
    def should_close_output(self) -> bool: ...
    def is_ready_to_execute(self) -> bool: ...
    def input(self) -> PyInput: ...
    def is_feature_supported(self, feature: int) -> bool: ...
    def take_notification(self) -> tuple[int, Any] | None: ...
    def take_aborted(self) -> list[int]: ...
    def get_state(
        self, scope: _Scope, key: str, *, name: str | None = None, parent_span: int | None = None, record: _Record = None
    ) -> int: ...
    def get_state_keys(
        self, scope: _Scope, *, name: str | None = None, parent_span: int | None = None, record: _Record = None
    ) -> int: ...
    def set_state(
        self,
        scope: _Scope,
        key: str,
        buffer: bytes,
        *,
        want_result: bool = False,
        name: str | None = None,
        parent_span: int | None = None,
        record: _Record = None,
    ) -> int | None: ...
    def clear_state(
        self,
        scope: _Scope,
        key: str,
        *,
        want_result: bool = False,
        name: str | None = None,
        parent_span: int | None = None,
        record: _Record = None,
    ) -> int | None: ...
    def clear_all_state(
        self,
        scope: _Scope,
        *,
        want_result: bool = False,
        name: str | None = None,
        parent_span: int | None = None,
        record: _Record = None,
    ) -> int | None: ...
    def call(
        self,
        service: str,
        handler: str,
        buffer: bytes,
        key: str | None = None,
        idempotency_key: str | None = None,
        headers: Sequence[Any] | None = None,
        delay: int | None = None,
        *,
        name: str | None = None,
        parent_span: int | None = None,
        record: _Record = None,
    ) -> Any: ...
    def send(
        self,
        service: str,
        handler: str,
        buffer: bytes,
        key: str | None = None,
        delay: int | None = None,
        idempotency_key: str | None = None,
        headers: Sequence[Any] | None = None,
        *,
        name: str | None = None,
        parent_span: int | None = None,
        record: _Record = None,
    ) -> int: ...
    def attach_invocation(
        self, invocation_id: str, *, name: str | None = None, parent_span: int | None = None, record: _Record = None
    ) -> int: ...
    def cancel_invocation(
        self,
        invocation_id: str,
        *,
        want_result: bool = False,
        name: str | None = None,
        parent_span: int | None = None,
        record: _Record = None,
    ) -> int | None: ...
    def signal_read(
        self,
        signal_name: str,
        index: int,
        *,
        name: str | None = None,
        parent_span: int | None = None,
        record: _Record = None,
    ) -> int: ...
    def signal_send(
        self,
        target_invocation_id: str,
        signal_name: str,
        value: bytes | Any | None = None,
        *,
        want_result: bool = False,
        name: str | None = None,
        parent_span: int | None = None,
        record: _Record = None,
    ) -> int | None: ...
    def flush(self, *, name: str | None = None, parent_span: int | None = None, record: _Record = None) -> int: ...
    def atomic_group_start(self) -> None: ...
    def atomic_group_end(
        self, *, want_result: bool = False, name: str | None = None, parent_span: int | None = None, record: _Record = None
    ) -> int | None: ...
    def atomic_group_abort(self) -> list[int]: ...
    def span_start(self, name: str, parent: int | None = None) -> int: ...
    def span_end(self, span: int, result: bytes | Any | None = None) -> None: ...
    def heartbeat(self) -> None: ...
    def end_success(self, buffer: bytes) -> None: ...
    def end_failure(self, failure: Any) -> None: ...
    def notify_error(self, error: str, stacktrace: str | None = None, delay_override_ms: int | None = None) -> None: ...
    def shutdown(self) -> None: ...
    def is_cancelled(self) -> bool: ...
    def state(self) -> str: ...

class FakeRuntimeViolation(AssertionError):
    """The SDK violated the explicit recovery protocol, as the fake runtime found (test builds only)."""

class _FakeRuntime:
    """Shared-core's fake Restate runtime for the explicit recovery protocol: one invocation, over as many attempts
    as it takes. In test builds only (Cargo feature ``fake_runtime``; ``src/fake_runtime.rs``)."""

    def __init__(
        self,
        *,
        invocation_id: str = ...,
        service: str = ...,
        handler: str = ...,
        key: str | None = ...,
        shared: bool = False,
        input: bytes | None = None,
        headers: Sequence[tuple[str, str]] = ...,
        idempotency_key: str | None = None,
        scope: str | None = None,
        limit_key: str | None = None,
        object_preload: str | Sequence[str] | None = None,
        invocation_preload: str | Sequence[str] | None = None,
        first_next_result_id: int = 1,
        max_consecutive_failures: int | None = None,
        max_idle_restarts: int = 3,
        features: Sequence[int] = ...,
    ) -> None: ...
    @staticmethod
    def request_headers() -> list[tuple[str, str]]: ...
    def content_type(self) -> str: ...
    def set_object_state(self, key: str, value: bytes) -> None: ...
    def set_invocation_state(self, key: str, value: bytes) -> None: ...
    def script_callee(self, service: str, handler: str, behavior: str, value: Any = None) -> None: ...
    def status(self) -> Any: ...
    def object_state(self) -> list[tuple[str, bytes]]: ...
    def invocation_state(self) -> list[tuple[str, bytes]]: ...
    def outcomes(self) -> list[tuple[Any, ...]]: ...
    def attempt_number(self) -> int: ...
    def next_result_id(self) -> int: ...
    def calls(self) -> list[dict[str, Any]]: ...
    def signals_sent(self) -> list[tuple[str, str, Any]]: ...
    def cancels_sent(self) -> list[str]: ...
    def violations(self) -> list[str]: ...
    def last_retry_delay(self) -> int | None: ...
    def is_closed(self) -> bool: ...
    def is_attempt_running(self) -> bool: ...
    def start_attempt(self) -> bytes: ...
    def receive(self, bytes: bytes) -> None: ...
    def take_output(self) -> bytes: ...
    def set_auto_apply(self, auto_apply: bool) -> None: ...
    def apply_pending(self, max: int) -> int: ...
    def shut_down(self, reason: str) -> None: ...
    def shutdown_answer_timed_out(self) -> None: ...
    def sdk_closed(self) -> None: ...
    def break_stream(self) -> None: ...
    def lose_attempt(self) -> None: ...
    def complete_call(self, invocation_id: str, output: Any = None) -> None: ...
    def send_signal(self, name: str, value: Any = None) -> None: ...
    def cancel(self) -> None: ...
    def resume(self) -> None: ...

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
SQL_COMMIT_BEGIN: int
SQL_COMMIT_END: int
SQL_COMMIT_ABORT: int

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
