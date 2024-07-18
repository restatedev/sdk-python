#
#  Copyright (c) 2023-2024 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Node.js/TypeScript,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""
wrap the restate._internal.PyVM class
"""
# pylint: disable=E1101

from dataclasses import dataclass
import typing
from restate._internal import PyVM, PyFailure, PySuspended, PyVoid # pylint: disable=import-error,no-name-in-module

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
class Failure:
    """
    Failure
    """
    code: int
    message: str

@dataclass
class NotReady:
    """
    NotReady
    """

class SuspendedException(Exception):
    """
    Suspended Exception
    """
    def __init__(self, *args: object) -> None:
        super().__init__(*args)

NOT_READY = NotReady()
SUSPENDED = SuspendedException()

AsyncResultType = typing.Optional[typing.Union[bytes, Failure, NotReady]]

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

    def notify_error(self, error: str):
        """Notify the virtual machine of an error."""
        self.vm.notify_error(error)

    def take_output(self) -> typing.Optional[bytes]:
        """Take the output from the virtual machine."""
        return self.vm.take_output()

    def notify_await_point(self, handle: int):
        """Notify the virtual machine of an await point."""
        self.vm.notify_await_point(handle)

    def is_ready_to_execute(self) -> bool:
        """Returns true when the VM is ready to operate."""
        return self.vm.is_ready_to_execute()

    def take_async_result(self, handle: typing.Any) -> AsyncResultType:
        """Take the result of an asynchronous operation."""
        result = self.vm.take_async_result(handle)
        if result is None:
            return NOT_READY
        if isinstance(result, PyVoid):
            # success with an empty value
            return None
        if isinstance(result, bytes):
            # success with a non empty value
            return result
        if isinstance(result, PyFailure):
            # a terminal failure
            code = result.code
            message = result.message
            return Failure(code, message)
        if isinstance(result, PySuspended):
            # the state machine had suspended
            raise SUSPENDED
        raise ValueError(f"Unknown result type: {result}")

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
            invocation_id=invocation_id,
            random_seed=random_seed,
            headers=headers,
            input_buffer=input_buffer,
            key=key)

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

    def sys_sleep(self, millis: int):
        """Ask to sleep for a given duration"""
        return self.vm.sys_sleep(millis)

    def sys_call(self,
                 service: str,
                 handler: str,
                 parameter: bytes,
                 key: typing.Optional[str] = None):
        """Call a service"""
        return self.vm.sys_call(service, handler, parameter, key)

    # pylint: disable=too-many-arguments
    def sys_send(self,
                 service: str,
                 handler: str,
                 parameter: bytes,
                 key: typing.Optional[str] = None,
                 delay: typing.Optional[int] = None) -> None:
        """send an invocation to a service (no response)"""
        self.vm.sys_send(service, handler, parameter, key, delay)

    def sys_run_enter(self, name: str) -> typing.Union[bytes, None, Failure]:
        """
        Enter a side effect

        Returns:
            None if the side effect was not journald.
            PyFailure if the side effect failed.
            bytes if the side effect was successful.
        """
        result = self.vm.sys_run_enter(name)
        if result is None:
            return None
        if isinstance(result, PyFailure):
            return Failure(result.code, result.message) # pylint: disable=protected-access
        assert isinstance(result, bytes)
        return result

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

    def sys_run_exit_success(self, output: bytes) -> int:
        """
        Exit a side effect

        Args:
            output: The output of the side effect.

        Returns:
            handle
        """
        return self.vm.sys_run_exit_success(output)

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

    def sys_run_exit_failure(self, output: Failure) -> int:
        """
        Exit a side effect

        Args:
            name: The name of the side effect.
            output: The output of the side effect.
        """
        res = PyFailure(output.code, output.message)
        return self.vm.sys_run_exit_failure(res)

    def sys_end(self):
        """
        This method is responsible for ending the system.

        It calls the `sys_end` method of the `vm` object.
        """
        self.vm.sys_end()
