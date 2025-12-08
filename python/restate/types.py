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
from types import UnionType
from typing import Any, Tuple, Literal, Union, get_args, get_origin

from restate.client_types import RestateClient


@dataclass
class HarnessEnvironment:
    """Information about the test environment"""

    ingress_url: str
    """The URL of the Restate ingress endpoint used in the test"""

    admin_api_url: str
    """The URL of the Restate admin API endpoint used in the test"""

    client: RestateClient
    """The Restate client connected to the ingress URL"""


def extract_core_type(annotation: Any) -> Tuple[Literal["optional", "simple"], Any]:
    """
    Extract the core type from a type annotation.

    Currently only supports Optional[T] types.
    """
    if annotation is None:
        return "simple", annotation

    origin = get_origin(annotation)
    args = get_args(annotation)

    if (origin is UnionType or Union) and len(args) == 2 and type(None) in args:
        non_none_type = args[0] if args[1] is type(None) else args[1]
        return "optional", non_none_type

    return "simple", annotation
