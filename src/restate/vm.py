"""
wrap the restate_sdk_python_core.PyVM class
"""
# pylint: disable=E1101

from dataclasses import dataclass
import typing
import restate_sdk_python_core

AsyncResultType = typing.Optional[typing.Union[bytes, typing.Tuple[int, str]]]

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
        result = self.vm.take_output()
        if isinstance(result, restate_sdk_python_core.PyTakeOutputResult.EOF):
            return None
        if isinstance(result, restate_sdk_python_core.PyTakeOutputResult.Buffer):
            return bytes(result._0)
        raise ValueError(f"Unknown result type: {result}")

    def on_ready_to_execute(self, fn: typing.Callable[[typing.Optional[Exception]], None]):
        """Register a callback to be called when the virtual machine is ready to execute."""
        self.vm.on_ready_to_execute(fn)

    def take_is_ready_to_execute(self) -> bool:
        """Take the result of an asynchronous operation."""
        return self.vm.take_is_ready_to_execute()

    def on_async_ready(self, handle: typing.Any, fn: typing.Callable[[], None]):
        """Register a callback to be called when the virtual machine is ready to execute."""
        self.vm.on_async_ready(handle, fn)

    def dispose_callbacks(self):
        """Clear all callbacks"""
        self.vm.dispose_callbacks()

    def take_async_result(self, handle: typing.Any) -> AsyncResultType:
        """Take the result of an asynchronous operation."""
        result = self.vm.take_async_result(handle)
        if isinstance(result, restate_sdk_python_core.PyValue.Void):
            return None
        if isinstance(result, restate_sdk_python_core.PyValue.Success):
            return result.value
        if isinstance(result, restate_sdk_python_core.PyValue.Failure):
            code = result.value.code
            message = result.value.message
            return (code, message)
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
        headers: typing.List[typing.Tuple[str, str]] = inp.headers
        input_buffer: bytes = bytes(inp.input)
        return Invocation(
            invocation_id=invocation_id,
            random_seed=random_seed,
            headers=headers,
            input_buffer=input_buffer)

    def sys_write_output(self, output: typing.Union[bytes, Failure]):
        """
        Writes the output to the system.

        Args:
           output (typing.Union[bytes, Failure]): The output to be written. It can be either a bytes or a Failure object.

        Returns:
            None
        """
        if isinstance(output, Failure):
            res = restate_sdk_python_core.PyNonEmptyValue.Failure(output.code, output.message)
        else:
            res = restate_sdk_python_core.PyNonEmptyValue.Success(output)
        self.vm.sys_write_output(res)

    def sys_end(self):
        """
        This method is responsible for ending the system.

        It calls the `sys_end` method of the `vm` object.
        """
        self.vm.sys_end()
