# Copyright (c) 2025 - Restate Software, Inc., Restate GmbH
#
# This file is part of the Restate SDK for Python,
# which is released under the MIT license.
#
# You can find a copy of the license in file LICENSE in the root
# directory of this repository or package, or at
# https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
"""
Retry policy configuration for handler invocations exposed in the discovery manifest (protocol v4+).

Note: You can set these fields only if you register this service against restate-server >= 1.5
and discovery protocol v4. Otherwise, service discovery will fail.
"""
from __future__ import annotations

from datetime import timedelta
from enum import Enum
from typing import Optional, TypedDict


class OnMaxAttempts(str, Enum):
    """Behavior when reaching max attempts."""
    PAUSE = "PAUSE"
    KILL = "KILL"


class InvocationRetryPolicy(TypedDict):
    """
    Retry policy used by Restate when retrying failed handler invocations.

    Fields:
      - initial_interval: Initial delay before the first retry attempt.
      - exponentiation_factor: Exponential backoff multiplier used to compute the next retry delay.
      - max_interval: Upper bound for any computed retry delay.
      - max_attempts: Maximum number of attempts before giving up retrying.
            The initial call counts as the first attempt.
      - on_max_attempts: Behavior when reaching max attempts (PAUSE or KILL).
    """

    initial_interval: Optional[timedelta]
    exponentiation_factor: Optional[float]
    max_interval: Optional[timedelta]
    max_attempts: Optional[int]
    on_max_attempts: Optional[OnMaxAttempts]
