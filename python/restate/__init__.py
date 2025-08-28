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
Restate SDK for Python
"""

from .service import Service
from .object import VirtualObject
from .workflow import Workflow

# types
from .context import Context, ObjectContext, ObjectSharedContext
from .context import WorkflowContext, WorkflowSharedContext
# pylint: disable=line-too-long
from .context import DurablePromise, RestateDurableFuture, RestateDurableCallFuture, RestateDurableSleepFuture, SendHandle, RunOptions
from .exceptions import TerminalError
from .asyncio import as_completed, gather, wait_completed, select

from .endpoint import app

from .logging import getLogger, RestateLoggingFilter

try:
    from .harness import test_harness # type: ignore
except ImportError:
    # we don't have the appropriate dependencies installed

    # pylint: disable=unused-argument, redefined-outer-name
    def test_harness(app, follow_logs = False, restate_image = ""): # type: ignore
        """a dummy harness constructor to raise ImportError"""
        raise ImportError("Install restate-sdk[harness] to use this feature")

__all__ = [
    "Service",
    "VirtualObject",
    "Workflow",
    "Context",
    "ObjectContext",
    "ObjectSharedContext",
    "WorkflowContext",
    "WorkflowSharedContext",
    "DurablePromise",
    "RestateDurableFuture",
    "RestateDurableCallFuture",
    "RestateDurableSleepFuture",
    "SendHandle",
    "RunOptions",
    "TerminalError",
    "app",
    "test_harness",
    "gather",
    "as_completed",
    "wait_completed",
    "select",
    "logging",
    "RestateLoggingFilter"
]
