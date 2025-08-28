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
This module contains the logging utilities for restate handlers.
"""
import logging

from .server_context import restate_context_is_replaying

# pylint: disable=C0103
def getLogger(name=None):
    """
    Wrapper for logging.getLogger returning a logger configured with RestateLoggingFilter

    :param name: the logger name, if any
    :return: the logger as returned by logging.getLogger, configured with the RestateLoggingFilter
    """
    logger = logging.getLogger(name)
    logger.addFilter(RestateLoggingFilter())
    return logger

# pylint: disable=R0903
class RestateLoggingFilter(logging.Filter):
    """
    Restate logging filter. This filter will filter out logs on replay
    """

    def filter(self, record):
        # First, apply the filter base logic
        if not super().filter(record):
            return False

        # Read the context variable, check if we're replaying
        if restate_context_is_replaying.get():
            return False

        # We're not replaying, all good pass the event
        return True
