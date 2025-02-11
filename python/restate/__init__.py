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
from .context import DurablePromise

from .endpoint import app

try:
    from .harness import test_harness
except ImportError:
    # we don't have the appropriate dependencies installed

    # pylint: disable=unused-argument, redefined-outer-name
    def test_harness(app, follow_logs = False, restate_image = ""): # type: ignore
        """a dummy harness constructor to raise ImportError"""
        raise ImportError("Install restate-sdk[harness] to use this feature")
