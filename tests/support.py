#
#  Copyright (c) 2023-2026 - Restate Software, Inc., Restate GmbH
#
#  This file is part of the Restate SDK for Python,
#  which is released under the MIT license.
#
#  You can find a copy of the license in file LICENSE in the root
#  directory of this repository or package, or at
#  https://github.com/restatedev/sdk-typescript/blob/main/LICENSE
#
"""What the test modules share: the Restate server they run against, and a gate between their threads."""

import asyncio
import os
import threading

RESTATE_IMAGE = os.environ.get("RESTATE_IMAGE", "ghcr.io/restatedev/restate:1.8.1")
"""The Restate server's image: pinned, so that a new release of Restate changes no test run until it is updated.

``RESTATE_IMAGE`` runs the tests against another (``ghcr.io/restatedev/restate:latest``, say).
"""


class Gate:
    """A gate that a test opens or closes from its own thread, for code waiting on the served app's event loop.

    The app of a test harness runs on a thread and an event loop of its own,
    so an ``asyncio.Event`` that the test sets would wake nothing there.
    """

    def __init__(self) -> None:
        self._open = threading.Event()

    def open(self) -> None:
        self._open.set()

    def close(self) -> None:
        self._open.clear()

    async def wait(self) -> None:
        """Wait until the gate is open; cancellable, as an ``asyncio.Event``'s wait is."""
        while not self._open.is_set():
            await asyncio.sleep(0.01)
