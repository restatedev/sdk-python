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
"""This module contains the restate exceptions"""

# pylint: disable=C0301

class TerminalError(Exception):
    """This exception is thrown to indicate that Restate should not retry."""
    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class SdkInternalBaseException(Exception):
    """This exception is internal, and you should not catch it.
    If you need to distinguish with other exceptions, use is_internal_exception."""
    def __init__(self, message: str) -> None:
        super().__init__(
            message +
"""
This exception is safe to ignore. If you see it, you might be using a try/catch all statement.

Don't do:
try:
  # Code
except:
  # This catches all exceptions, including the SdkInternalBaseException!

Do instead:
try:
  # Code
except TerminalError:
  # In Restate handlers you typically want to catch TerminalError only

Or remove the try/except altogether if you don't need it.
For further info on error handling, refer to https://docs.restate.dev/develop/python/error-handling
""")

class SuspendedException(SdkInternalBaseException):
    """This exception is raised to indicate that the execution is suspended"""
    def __init__(self) -> None:
        super().__init__("Invocation got suspended, Restate will resume this invocation when progress can be made.")

class SdkInternalException(SdkInternalBaseException):
    """This exception is raised to indicate that the execution raised a retryable error."""
    def __init__(self) -> None:
        super().__init__("Invocation attempt raised a retryable error.\n"
                         "Restate will retry executing this invocation from the point where it left off.")


def is_internal_exception(e) -> bool:
    """Returns true if the exception is an internal Restate exception"""
    return isinstance(e, SdkInternalBaseException)
