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
"""Markers of the tests, by what they need: ``server`` for those that run against a Restate server, in Docker.

``pytest -m "not server"`` (``just test-fast``) runs the others: in-process, in seconds, without Docker.
"""

import pytest

SERVED = frozenset({"harness", "restate_test_harness"})
"""The fixtures that start a Restate server. Tests that start one otherwise are marked ``server`` themselves."""


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "server: runs against a Restate server, in Docker")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if SERVED & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.server)
