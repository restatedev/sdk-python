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
import subprocess
import sys

import pytest

pytest.importorskip("pydantic_ai")


def run_without_mcp(code: str) -> subprocess.CompletedProcess[str]:
    """Run code in a fresh interpreter where the MCP client packages can't be imported,
    as with pydantic-ai-slim installed without its `mcp` extra."""
    prelude = 'import sys\nsys.modules["mcp"] = None\nsys.modules["fastmcp"] = None\n'
    return subprocess.run([sys.executable, "-c", prelude + code], capture_output=True, text=True)


def test_import_without_mcp():
    result = run_without_mcp("import restate.ext.pydantic")
    assert result.returncode == 0, result.stderr


def test_agent_with_function_tools_without_mcp():
    result = run_without_mcp(
        """
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from restate.ext.pydantic import RestateAgent, RestateContextRunToolSet

agent = Agent(TestModel())

@agent.tool_plain
def add(a: int, b: int) -> int:
    return a + b

restate_agent = RestateAgent(agent, auto_wrap_tools=True)
assert any(isinstance(t, RestateContextRunToolSet) for t in restate_agent.toolsets), restate_agent.toolsets
"""
    )
    assert result.returncode == 0, result.stderr


def test_agent_wraps_mcp_toolsets():
    mcp = pytest.importorskip("pydantic_ai.mcp", exc_type=ImportError)
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from restate.ext.pydantic import RestateAgent
    from restate.ext.pydantic._mcp_toolset import RestateMCPToolset

    agent = Agent(TestModel(), toolsets=[mcp.MCPToolset("http://localhost:8000/mcp")])
    restate_agent = RestateAgent(agent)
    assert any(isinstance(t, RestateMCPToolset) for t in restate_agent.toolsets)
