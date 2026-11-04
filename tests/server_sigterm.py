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
"""The endpoint's SIGTERM handler leaves the ASGI server's own in place."""

import asyncio
import os
import signal
import sys

import pytest

from restate.server import install_shutdown_hook

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


def test_sigterm_reaches_the_server_and_the_endpoint():
    async def main() -> list[str]:
        loop = asyncio.get_running_loop()
        heard: list[str] = []
        both = asyncio.Event()

        def hear(who: str) -> None:
            heard.append(who)
            if len(heard) == 2:
                both.set()

        # The server's handler, as hypercorn installs it, and then the endpoint's, on its first request.
        loop.add_signal_handler(signal.SIGTERM, hear, "server")
        install_shutdown_hook(loop, lambda: hear("endpoint"), signal.SIGTERM, chain=True)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(both.wait(), 5)
        loop.remove_signal_handler(signal.SIGTERM)
        return heard

    previous = signal.getsignal(signal.SIGTERM)
    try:
        assert sorted(asyncio.run(main())) == ["endpoint", "server"]
    finally:
        signal.signal(signal.SIGTERM, previous)
