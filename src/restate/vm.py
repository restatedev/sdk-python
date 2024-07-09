"""
wrap the restate_sdk_python_core.PyVM class
"""
# pylint: disable=E1101

from dataclasses import dataclass
import typing
import restate_sdk_python_core

@dataclass
class Invocation:
    """
    Invocation dataclass
    """
    invocation_id: str
    random_seed: int
    headers: typing.List[typing.Tuple[str, str]]
    input_buffer: bytes


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

AsyncResultType = typing.Optional[typing.Union[bytes, Failure, NotReady]]

class VMWrapper:
    """
    A wrapper class for the restate_sdk_python_core.PyVM class.
    It provides a type-friendly interface to our shared vm. 
    """

    def __init__(self, headers: typing.List[typing.Tuple[str, str]]):
        self.vm = restate_sdk_python_core.PyVM(headers)

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

    def dispose_callbacks(self):
        """Clear all callbacks"""
        self.vm.dispose_callbacks()

    def take_async_result(self, handle: typing.Any) -> AsyncResultType:
        """Take the result of an asynchronous operation."""
        try:
            result = self.vm.take_async_result(handle)
            if not result:
                return NOT_READY
            if isinstance(result, restate_sdk_python_core.PyVoid):
                return None
            if isinstance(result, bytes):
                return result
            if isinstance(result, restate_sdk_python_core.PyFailure):
                code = result._0.code # pylint: disable=protected-access
                message = result._0.message # pylint: disable=protected-access
                return Failure(code, message)
            raise ValueError(f"Unknown result type: {result}")
        except restate_sdk_python_core.SuspendedException:
            raise SuspendedException() # pylint: disable=raise-missing-from

    def sys_input(self) -> Invocation:
        """
            Retrieves the system input from the virtual machine.

            Returns:
                An instance of the Invocation class containing the system input.
        """
        inp = self.vm.sys_input()
        invocation_id: str = inp.invocation_id
        random_seed: int = inp.random_seed
        headers: typing.List[typing.Tuple[str, str]] = inp.headers
        input_buffer: bytes = bytes(inp.input)
        return Invocation(
            invocation_id=invocation_id,
            random_seed=random_seed,
            headers=headers,
            input_buffer=input_buffer)

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
        res = restate_sdk_python_core.PyFailure(output.code, output.message)
        self.vm.sys_write_output_failure(res)


    def sys_get(self, name) -> int:
        """
        Retrieves a key-value binding.

        Args:
            name: The name of the value to be retrieved.

        Returns:
            The value associated with the given name.
        """
        return self.vm.sys_get(name)

    def sys_set(self, name: str, value: bytes):
        """
        Sets a key-value binding.

        Args:
            name: The name of the value to be set.
            value: The value to be set.

        Returns:
            None
        """
        self.vm.sys_set(name, value)

    def sys_end(self):
        """
        This method is responsible for ending the system.

        It calls the `sys_end` method of the `vm` object.
        """
        self.vm.sys_end()
