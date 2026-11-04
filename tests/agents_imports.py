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
"""
restate.sessions and restate.agents need no package beyond the SDK's own: a
framework's comes with its extra, and pydantic, which tools take their schemas
from, with any of them.
"""

# pylint: disable=missing-function-docstring

import subprocess
import sys

WITHOUT_PYDANTIC = """
import sys
sys.modules["pydantic"] = None
sys.modules["pydantic_core"] = None
import restate, restate.sessions, restate.sessions.internal, restate.agents, restate.agents.internal
try:
    restate.agents.tool(lambda city: city)
except ImportError as error:
    print(error)
"""


def test_sessions_and_agents_import_without_pydantic_and_a_tool_says_what_it_needs():
    ran = subprocess.run([sys.executable, "-c", WITHOUT_PYDANTIC], capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stderr
    assert "tools take their inputs' schemas from pydantic" in ran.stdout


WITHOUT_LANGGRAPH = """
import sys
for name in ("langgraph", "langchain_core"):
    sys.modules[name] = None
import restate.agents, restate.agents.internal
try:
    import restate.agents.langgraph
except ImportError as error:
    print(error)
"""


def test_agents_import_without_langgraph_and_its_integration_names_its_extra():
    ran = subprocess.run([sys.executable, "-c", WITHOUT_LANGGRAPH], capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stderr
    assert "install the langgraph extra, pip install restate-sdk[langgraph]" in ran.stdout


WITHOUT_PYDANTIC_AI = """
import sys
sys.modules["pydantic_ai"] = None
import restate.agents, restate.agents.internal
try:
    import restate.agents.pydantic_ai
except ImportError as error:
    print(error)
"""


def test_agents_import_without_pydantic_ai_and_its_integration_names_its_extra():
    ran = subprocess.run([sys.executable, "-c", WITHOUT_PYDANTIC_AI], capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stderr
    assert "install the pydantic_ai extra, pip install restate-sdk[pydantic_ai]" in ran.stdout


WITHOUT_ANTHROPIC = """
import sys
sys.modules["anthropic"] = None
import restate.agents, restate.agents.internal
try:
    import restate.agents.anthropic
except ImportError as error:
    print(error)
"""


def test_agents_import_without_anthropic_and_its_integration_names_its_extra():
    ran = subprocess.run([sys.executable, "-c", WITHOUT_ANTHROPIC], capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stderr
    assert "install the anthropic extra, pip install restate-sdk[anthropic]" in ran.stdout
