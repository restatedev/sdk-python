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

from contextlib import contextmanager
import typing

from restate.server_types import RestateAppT
from restate.types import TestHarnessEnvironment

from .service import Service
from .object import VirtualObject
from .workflow import Workflow

# types
from .context import Context, ObjectContext, ObjectSharedContext
from .context import WorkflowContext, WorkflowSharedContext
from .retry_policy import InvocationRetryPolicy

# pylint: disable=line-too-long
from .context import (
    DurablePromise,
    RestateDurableFuture,
    RestateDurableCallFuture,
    RestateDurableSleepFuture,
    SendHandle,
    RunOptions,
)
from .exceptions import TerminalError, SdkInternalBaseException, is_internal_exception
from .asyncio import as_completed, gather, wait_completed, select

from .endpoint import app

from .logging import getLogger, RestateLoggingFilter


try:
    from .harness import create_test_harness, test_harness  # type: ignore
except ImportError:
    # we don't have the appropriate dependencies installed

    # pylint: disable=unused-argument, redefined-outer-name
    @contextmanager
    def create_test_harness(
        app: RestateAppT,
        follow_logs: bool = False,
        restate_image: str = "restatedev/restate:latest",
        always_replay: bool = False,
        disable_retries: bool = False,
    ) -> typing.Generator[TestHarnessEnvironment, None, None]:
        """a dummy harness constructor to raise ImportError"""
        raise ImportError("Install restate-sdk[harness] to use this feature")

    def test_harness(
        app: RestateAppT,
        follow_logs: bool = False,
        restate_image: str = "restatedev/restate:latest",
        always_replay: bool = False,
        disable_retries: bool = False,
    ):
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
    "create_test_harness",
    "test_harness",
    "gather",
    "as_completed",
    "wait_completed",
    "select",
    "logging",
    "RestateLoggingFilter",
    "InvocationRetryPolicy",
    "SdkInternalBaseException",
    "is_internal_exception",
    "getLogger",
]
