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

class TerminalError(Exception):
    """This exception is raised to indicate a termination of the execution"""

    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code

class AbortedExecutionException(BaseException):
    """This exception is raised to indicate that the execution is aborted.
    You should never catch it!"""
    def __init__(self) -> None:
        super().__init__("AbortedExecutionException: You should never catch this exception, "
                         "and you should not call any Context method "
                         "after this exception is thrown.")
