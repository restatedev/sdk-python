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
"""
wrap the restate._internal.PyVM class
"""

# pylint: disable=E1101,R0917
# pylint: disable=too-many-arguments
# pylint: disable=too-few-public-methods
from typing import List, Optional, Union
from datetime import timedelta

from dataclasses import dataclass, field
import typing
from restate._internal import (
    PyVM,
    PyHeader,
    PyFailure,
    VMException,
    PySuspended,
    PyVoid,
    PyStateKeys,
    PyExponentialRetryConfig,
    PyDoProgressAnyCompleted,
    PyDoProgressWaitExternalProgress,
    PyDoProgressExecuteRun,
    PyDoProgressCancelSignalReceived,
    PyUnresolvedFuture,
    CANCEL_NOTIFICATION_HANDLE,
)  # pylint: disable=import-error,no-name-in-module,line-too-long


@dataclass
class Invocation:
    """
    Invocation dataclass
    """

    invocation_id: str
    random_seed: int
    headers: typing.List[typing.Tuple[str, str]]
    input_buffer: bytes
    key: str


@dataclass
class RunRetryConfig:
    """
    Exponential Retry Configuration

    All duration/interval values are in milliseconds.
    """

    initial_interval: typing.Optional[int] = None
    max_attempts: typing.Optional[int] = None
    max_duration: typing.Optional[int] = None
    max_interval: typing.Optional[int] = None
    interval_factor: typing.Optional[float] = None


@dataclass
class Failure:
    """
    Failure
    """

    code: int
    message: str
    stacktrace: typing.Optional[str] = None


@dataclass
class NotReady:
    """
    NotReady
    """


NOT_READY = NotReady()
CANCEL_HANDLE = CANCEL_NOTIFICATION_HANDLE

NotificationType = typing.Optional[typing.Union[bytes, Failure, NotReady, list[str], str]]


class Suspended:
    """
    Represents a suspended error
    """


SUSPENDED = Suspended()


class DoProgressAnyCompleted:
    """
    Represents a notification that any of the handles has completed.
    """


class DoProgressWaitExternalProgress:
    """
    Represents a notification that external progress is required
    (either new input from the server or a pending run proposal).
    """


class DoProgressExecuteRun:
    """
    Represents a notification that a run needs to be executed.
    """

    handle: int

    def __init__(self, handle):
        self.handle = handle


class DoProgressCancelSignalReceived:
    """
    Represents a notification that a cancel signal has been received
    """


DO_PROGRESS_ANY_COMPLETED = DoProgressAnyCompleted()
DO_PROGRESS_WAIT_EXTERNAL_PROGRESS = DoProgressWaitExternalProgress()
DO_PROGRESS_CANCEL_SIGNAL_RECEIVED = DoProgressCancelSignalReceived()

DoProgressResult = typing.Union[
    DoProgressAnyCompleted,
    DoProgressWaitExternalProgress,
    DoProgressExecuteRun,
    DoProgressCancelSignalReceived,
]


@dataclass(frozen=True)
class SingleUnresolvedFuture:
    """A single leaf handle."""

    handle: int


@dataclass(frozen=True)
class FirstCompletedUnresolvedFuture:
    """first child to complete (success or failure) wins."""

    children: List["UnresolvedFuture"] = field(default_factory=list)


@dataclass(frozen=True)
class AllCompletedUnresolvedFuture:
    """wait for all children to complete."""

    children: List["UnresolvedFuture"] = field(default_factory=list)


UnresolvedFuture = Union[
    SingleUnresolvedFuture,
    FirstCompletedUnresolvedFuture,
    AllCompletedUnresolvedFuture,
]


def _unresolved_future_to_pyo3(uf: UnresolvedFuture) -> PyUnresolvedFuture:
    """Recursively convert a Python-side UnresolvedFuture dataclass to its PyO3 pyclass."""
    if isinstance(uf, SingleUnresolvedFuture):
        return PyUnresolvedFuture.single(uf.handle)
    if isinstance(uf, FirstCompletedUnresolvedFuture):
        return PyUnresolvedFuture.first_completed([_unresolved_future_to_pyo3(c) for c in uf.children])
    if isinstance(uf, AllCompletedUnresolvedFuture):
        return PyUnresolvedFuture.all_completed([_unresolved_future_to_pyo3(c) for c in uf.children])
    raise TypeError(f"Unknown UnresolvedFuture variant: {type(uf).__name__}")


# pylint: disable=too-many-public-methods
class VMWrapper:
    """
    A wrapper class for the restate_sdk._internal.PyVM class.
    It provides a type-friendly interface to our shared vm.
    """

    def __init__(self, headers: typing.List[typing.Tuple[str, str]]):
        self.vm = PyVM(headers)

    def get_response_head(self) -> typing.Tuple[int, typing.Iterable[typing.Tuple[str, str]]]:
        """
        Retrieves the response head from the virtual machine.

        Returns:
            A tuple containing the status code and a list of header tuples.
        """
        result = self.vm.get_response_head()
        return (result.status_code, result.headers)

    def notify_input(self, input_buf: bytes):
        """Send input to the virtual machine."""
        self.vm.notify_input(input_buf)

    def notify_input_closed(self):
        """Notify the virtual machine that the input has been closed."""
        self.vm.notify_input_closed()

    def notify_error(self, error: str, stacktrace: str, delay_override: Optional[timedelta] = None):
        """Notify the virtual machine of an error."""
        self.vm.notify_error(
            error, stacktrace, int(delay_override.total_seconds() * 1000) if delay_override is not None else None
        )

    def take_output(self) -> typing.Optional[bytes]:
        """Take the output from the virtual machine."""
        return self.vm.take_output()

    def is_ready_to_execute(self) -> bool:
        """Returns true when the VM is ready to operate."""
        return self.vm.is_ready_to_execute()

    def is_completed(self, handle: int) -> bool:
        """Returns true when the notification handle is completed and hasn't been taken yet."""
        return self.vm.is_completed(handle)

    # pylint: disable=R0911
    def do_progress(self, unresolved_future: UnresolvedFuture) -> typing.Union[DoProgressResult, Exception, Suspended]:
        """Do progress with notifications."""
        try:
            result = self.vm.do_progress(_unresolved_future_to_pyo3(unresolved_future))
        except VMException as e:
            return e
        if isinstance(result, PySuspended):
            return SUSPENDED
        if isinstance(result, PyDoProgressAnyCompleted):
            return DO_PROGRESS_ANY_COMPLETED
        if isinstance(result, PyDoProgressWaitExternalProgress):
            return DO_PROGRESS_WAIT_EXTERNAL_PROGRESS
        if isinstance(result, PyDoProgressExecuteRun):
            return DoProgressExecuteRun(result.handle)
        if isinstance(result, PyDoProgressCancelSignalReceived):
            return DO_PROGRESS_CANCEL_SIGNAL_RECEIVED
        return ValueError(f"Unknown progress type: {result}")

    def take_notification(self, handle: int) -> typing.Union[NotificationType, Exception, Suspended]:
        """Take the result of an asynchronous operation."""
        try:
            result = self.vm.take_notification(handle)
        except VMException as e:
            return e
        if isinstance(result, PySuspended):
            return SUSPENDED
        if result is None:
            return NOT_READY
        if isinstance(result, PyVoid):
            # success with an empty value
            return None
        if isinstance(result, bytes):
            # success with a non-empty value
            return result
        if isinstance(result, PyStateKeys):
            # success with state keys
            return result.keys
        if isinstance(result, str):
            # success with invocation id
            return result
        if isinstance(result, PyFailure):
            # a terminal failure
            code = result.code
            message = result.message
            return Failure(code, message)
        return ValueError(f"Unknown result type: {result}")

    def sys_input(self) -> Invocation:
        """
        Retrieves the system input from the virtual machine.

        Returns:
            An instance of the Invocation class containing the system input.
        """
        inp = self.vm.sys_input()
        invocation_id: str = inp.invocation_id
        random_seed: int = inp.random_seed
        headers: typing.List[typing.Tuple[str, str]] = [(h.key, h.value) for h in inp.headers]
        input_buffer: bytes = bytes(inp.input)
        key: str = inp.key

        return Invocation(
            invocation_id=invocation_id, random_seed=random_seed, headers=headers, input_buffer=input_buffer, key=key
        )

    def sys_write_output_success(self, output: bytes):
        """
        Writes the output to the system.

        Args:
          output: The output to be written. It can be either a bytes or a Failure object.

        Returns:
            None
        """
        self.vm.sys_write_output_success(output)

    def sys_write_output_failure(self, output: Failure):
        """
        Writes the output to the system.

        Args:
          output: The output to be written. It can be either a bytes or a Failure object.

        Returns:
            None
        """
        res = PyFailure(output.code, output.message)
        self.vm.sys_write_output_failure(res)

    def sys_get_state(self, name) -> int:
        """
        Retrieves a key-value binding.

        Args:
            name: The name of the value to be retrieved.

        Returns:
            The value associated with the given name.
        """
        return self.vm.sys_get_state(name)

    def sys_get_state_keys(self) -> int:
        """
        Retrieves all keys.

        Returns:
            The state keys
        """
        return self.vm.sys_get_state_keys()

    def sys_set_state(self, name: str, value: bytes):
        """
        Sets a key-value binding.

        Args:
            name: The name of the value to be set.
            value: The value to be set.

        Returns:
            None
        """
        self.vm.sys_set_state(name, value)

    def sys_clear_state(self, name: str):
        """Clear the state associated with the given name."""
        self.vm.sys_clear_state(name)

    def sys_clear_all_state(self):
        """Clear the state associated with the given name."""
        self.vm.sys_clear_all_state()

    def sys_sleep(self, millis: int, name: typing.Optional[str] = None):
        """Ask to sleep for a given duration"""
        return self.vm.sys_sleep(millis, name)

    def sys_call(
        self,
        service: str,
        handler: str,
        parameter: bytes,
        key: typing.Optional[str] = None,
        idempotency_key: typing.Optional[str] = None,
        headers: typing.Optional[typing.List[typing.Tuple[str, str]]] = None,
    ):
        """Call a service"""
        py_headers = [PyHeader(key=h[0], value=h[1]) for h in headers] if headers else None
        return self.vm.sys_call(service, handler, parameter, key, idempotency_key, py_headers)

    # pylint: disable=too-many-arguments
    def sys_send(
        self,
        service: str,
        handler: str,
        parameter: bytes,
        key: typing.Optional[str] = None,
        delay: typing.Optional[int] = None,
        idempotency_key: typing.Optional[str] = None,
        headers: typing.Optional[typing.List[typing.Tuple[str, str]]] = None,
    ) -> int:
        """
        send an invocation to a service, and return the handle
        to the promise that will resolve with the invocation id
        """
        py_headers = [PyHeader(key=h[0], value=h[1]) for h in headers] if headers else None
        return self.vm.sys_send(service, handler, parameter, key, delay, idempotency_key, py_headers)

    def sys_run(self, name: str) -> int:
        """
        Register a run
        """
        return self.vm.sys_run(name)

    def sys_awakeable(self) -> typing.Tuple[str, int]:
        """
        Return a fresh awaitable
        """
        return self.vm.sys_awakeable()

    def sys_resolve_awakeable(self, name: str, value: bytes):
        """
        Resolve
        """
        self.vm.sys_complete_awakeable_success(name, value)

    def sys_reject_awakeable(self, name: str, failure: Failure):
        """
        Reject
        """
        py_failure = PyFailure(failure.code, failure.message)
        self.vm.sys_complete_awakeable_failure(name, py_failure)

    def propose_run_completion_success(self, handle: int, output: bytes) -> None:
        """
        Exit a side effect with a success value.
        """
        self.vm.propose_run_completion_success(handle, output)

    def sys_get_promise(self, name: str) -> int:
        """Returns the promise handle"""
        return self.vm.sys_get_promise(name)

    def sys_peek_promise(self, name: str) -> int:
        """Peek into the workflow promise"""
        return self.vm.sys_peek_promise(name)

    def sys_complete_promise_success(self, name: str, value: bytes) -> int:
        """Complete the promise"""
        return self.vm.sys_complete_promise_success(name, value)

    def sys_complete_promise_failure(self, name: str, failure: Failure) -> int:
        """reject the promise on failure"""
        res = PyFailure(failure.code, failure.message)
        return self.vm.sys_complete_promise_failure(name, res)

    def propose_run_completion_failure(self, handle: int, output: Failure) -> None:
        """
        Exit a side effect with a terminal failure.
        """
        res = PyFailure(output.code, output.message)
        self.vm.propose_run_completion_failure(handle, res)

    # pylint: disable=line-too-long
    def propose_run_completion_transient(
        self, handle: int, failure: Failure, attempt_duration_ms: int, config: RunRetryConfig
    ):
        """
        Exit a side effect with a transient Error.
        This requires a retry policy to be provided.
        """
        py_failure = PyFailure(failure.code, failure.message, failure.stacktrace)
        py_config = PyExponentialRetryConfig(
            config.initial_interval,
            config.max_attempts,
            config.max_duration,
            config.max_interval,
            config.interval_factor,
        )
        self.vm.propose_run_completion_failure_transient(handle, py_failure, attempt_duration_ms, py_config)

    def propose_run_completion_transient_with_delay_override(
        self,
        handle: int,
        failure: Failure,
        attempt_duration_ms: int,
        delay_override: timedelta | None,
        max_retry_attempts_override: int | None,
        max_retry_duration_override: timedelta | None,
    ):
        """
        Exit a side effect with a transient Error and override the retry policy with explicit parameters.
        """
        py_failure = PyFailure(failure.code, failure.message, failure.stacktrace)
        self.vm.propose_run_completion_failure_transient_with_delay_override(
            handle,
            py_failure,
            attempt_duration_ms,
            int(delay_override.total_seconds() * 1000) if delay_override else None,
            max_retry_attempts_override,
            int(max_retry_duration_override.total_seconds() * 1000) if max_retry_duration_override else None,
        )

    def sys_end(self):
        """
        This method is responsible for ending the system.

        It calls the `sys_end` method of the `vm` object.
        """
        self.vm.sys_end()

    def sys_cancel(self, invocation_id: str):
        """
        Cancel a running invocation
        """
        self.vm.sys_cancel(invocation_id)

    def attach_invocation(self, invocation_id: str) -> int:
        """
        Attach to an invocation
        """
        return self.vm.attach_invocation(invocation_id)

    def is_replaying(self) -> bool:
        """Returns true if the state machine is replaying."""
        return self.vm.is_replaying()
