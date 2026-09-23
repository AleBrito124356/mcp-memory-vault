"""Release metadata must agree: pyproject, __version__ and server.json."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mcp_memory_vault import __version__  # noqa: E402


def test_versions_agree():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)
    assert match, "pyproject.toml has no version"
    assert match.group(1) == __version__

    manifest = json.loads((REPO / "server.json").read_text(encoding="utf-8"))
    assert manifest["version"] == __version__
    assert {pkg["version"] for pkg in manifest["packages"]} == {__version__}


def test_mcp_dependency_is_bounded():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    deps = re.search(r"^dependencies = \[(.*?)\]", pyproject, re.MULTILINE | re.DOTALL)
    assert deps, "pyproject.toml has no dependencies list"
    mcp_spec = next(d for d in re.findall(r'"([^"]+)"', deps.group(1)) if d.startswith("mcp"))
    assert "<" in mcp_spec, f"unbounded mcp dependency: {mcp_spec}"
