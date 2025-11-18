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

from contextlib import asynccontextmanager
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
from .client_types import RestateClient, RestateClientSendHandle

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
    @asynccontextmanager
    def create_test_harness(
        app: RestateAppT,
        follow_logs: bool = False,
        restate_image: str = "restatedev/restate:latest",
        always_replay: bool = False,
        disable_retries: bool = False,
    ) -> typing.AsyncGenerator[TestHarnessEnvironment, None]:
        """a dummy harness constructor to raise ImportError. Install restate-sdk[harness] to use this feature"""
        raise ImportError("Install restate-sdk[harness] to use this feature")

    @typing.no_type_check
    def test_harness(
        app: RestateAppT,
        follow_logs: bool = False,
        restate_image: str = "restatedev/restate:latest",
        always_replay: bool = False,
        disable_retries: bool = False,
    ):
        """a dummy harness constructor to raise ImportError. Install restate-sdk[harness] to use this feature"""
        raise ImportError("Install restate-sdk[harness] to use this feature")  # type: ignore


try:
    from .client import create_client
except ImportError:
    # we don't have the appropriate dependencies installed

    @asynccontextmanager
    async def create_client(
        ingress: str, headers: typing.Optional[dict] = None
    ) -> typing.AsyncGenerator[RestateClient, None]:
        """a dummy client constructor to raise ImportError. Install restate-sdk[client] to use this feature"""
        raise ImportError("Install restate-sdk[client] to use this feature")
        yield  # type: ignore


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
    "RestateClient",
    "RestateClientSendHandle",
    "create_client",
]
