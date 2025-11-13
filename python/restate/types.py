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

# pylint: disable=R0917
# pylint: disable=R0801
"""
This represents common types used throughout the Restate SDK for Python.
"""

from dataclasses import dataclass


@dataclass
class TestHarnessEnvironment:
    """Information about the test environment"""

    ingress_url: str
    """The URL of the Restate ingress endpoint used in the test"""

    admin_api_url: str
    """The URL of the Restate admin API endpoint used in the test"""
