import subprocess
import sys

import pytest

pytest.importorskip("pydantic_ai")


def test_import_without_pydantic_ai_mcp():
    """restate.ext.pydantic must be importable without the pydantic-ai ``mcp`` extra."""
    code = (
        "import sys\n"
        "sys.modules['pydantic_ai.mcp'] = None  # simulate the missing optional dependency\n"
        "import restate.ext.pydantic as ext\n"
        "assert ext.RestateAgent is not None\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
