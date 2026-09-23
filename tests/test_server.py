"""Server-level tests: calls go through the real MCP protocol, in process.

They run on both major versions of the SDK (mcp 1.x FastMCP and mcp 2.x
MCPServer), which is what the compat layer in server.py promises.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mcp_memory_vault import server  # noqa: E402
from mcp_memory_vault.core import MemoryVault  # noqa: E402


@pytest.fixture
def vault(tmp_path):
    v = MemoryVault(db_path=tmp_path / "server.db")
    previous = server.set_vault(v)
    yield v
    server.set_vault(previous)
    v.close()


@asynccontextmanager
async def _session():
    """An initialized client session connected in memory to our server."""
    if server.MCP_MAJOR >= 2:
        from mcp.client import Client

        async with Client(server.mcp) as client:
            yield client
    else:
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(server.mcp._mcp_server) as session:
            yield session


def _is_error(result) -> bool:
    return bool(getattr(result, "is_error", getattr(result, "isError", False)))


def _payload(result) -> dict:
    return json.loads(result.content[0].text)


def _run(coro):
    return asyncio.run(coro)


def _call(name: str, arguments: dict):
    async def go():
        async with _session() as session:
            return await session.call_tool(name, arguments)

    return _run(go())


def _input_schema(tool) -> dict:
    return getattr(tool, "input_schema", None) or tool.inputSchema


def _hint(annotations, snake: str, camel: str):
    return getattr(annotations, snake, getattr(annotations, camel, None))


# ---------------------------------------------------------------------------


def test_importing_the_server_has_no_side_effects(tmp_path):
    db = tmp_path / "not-yet" / "vault.db"
    env = dict(os.environ, MEMORY_VAULT_DB=str(db), PYTHONPATH=str(REPO))
    subprocess.run(
        [sys.executable, "-c", "import mcp_memory_vault.server"],
        env=env,
        cwd=REPO,
        check=True,
        timeout=60,
    )
    assert not db.parent.exists(), "importing server.py must not create the vault"


def test_vault_opens_lazily_at_env_path(tmp_path, monkeypatch):
    db = tmp_path / "lazy.db"
    monkeypatch.setenv("MEMORY_VAULT_DB", str(db))
    previous = server.set_vault(None)
    try:
        assert not db.exists()
        vault = server.get_vault()
        assert vault.db_path == db
        assert db.exists()
        assert server.get_vault() is vault
        vault.close()
    finally:
        server.set_vault(previous)


def test_tools_are_listed_with_annotations_and_documented_arguments(vault):
    async def go():
        async with _session() as session:
            return (await session.list_tools()).tools

    tools = {tool.name: tool for tool in _run(go())}
    assert set(tools) == {
        "remember",
        "recall",
        "forget",
        "list_memories",
        "update_memory",
        "memory_stats",
        "export_memories",
        "import_memories",
    }
    for name in ("recall", "list_memories", "memory_stats", "export_memories"):
        assert _hint(tools[name].annotations, "read_only_hint", "readOnlyHint") is True, name
    assert _hint(tools["forget"].annotations, "destructive_hint", "destructiveHint") is True
    assert _hint(tools["remember"].annotations, "destructive_hint", "destructiveHint") is False
    for tool in tools.values():
        assert tool.title, f"{tool.name} has no title"
        for arg, spec in _input_schema(tool).get("properties", {}).items():
            assert spec.get("description"), f"{tool.name}.{arg} has no description"


def test_remember_then_recall_through_the_protocol(vault):
    async def go():
        async with _session() as session:
            stored = await session.call_tool(
                "remember",
                {
                    "content": "Customer ACME prefers deploys on Fridays",
                    "namespace": "support",
                    "tags": ["customer", "deploy"],
                },
            )
            found = await session.call_tool("recall", {"query": "ACME deploy"})
            return stored, found

    stored, found = _run(go())
    assert not _is_error(stored)
    assert _payload(stored)["deduplicated"] is False
    hits = _payload(found)["hits"]
    assert len(hits) == 1
    assert "[ACME]" in hits[0]["snippet"]
    # The tool wrote to the vault the fixture installed, not to ~/.
    assert vault.memory_stats()["total_memories"] == 1


def test_core_errors_reach_the_model_with_their_hint(vault):
    result = _call("forget", {"memory_id": 999})
    assert _is_error(result)
    text = result.content[0].text
    assert "Memory 999 not found" in text
    assert "list_memories" in text  # the actionable part must not be swallowed


def test_validation_errors_reach_the_model(vault):
    result = _call("remember", {"content": "   "})
    assert _is_error(result)
    assert "content must not be empty" in result.content[0].text

    result = _call("update_memory", {"memory_id": 1})
    assert _is_error(result)
    assert "Nothing to update" in result.content[0].text

    result = _call("update_memory", {"memory_id": 42, "content": "new text"})
    assert _is_error(result)
    assert "Memory 42 not found" in result.content[0].text


def test_concurrent_tool_calls_are_all_stored(vault):
    """mcp 2.x runs sync tools on worker threads: nothing may be lost."""

    async def go():
        async with _session() as session:
            results = await asyncio.gather(
                *(
                    session.call_tool("remember", {"content": f"parallel fact number {i}"})
                    for i in range(40)
                )
            )
            return results

    results = _run(go())
    assert not any(_is_error(r) for r in results)
    assert vault.memory_stats()["total_memories"] == 40


def test_update_memory_tool_moves_retags_and_refuses_duplicates(vault):
    alpha = vault.remember("Fact alpha", namespace="n", tags=["Draft", "x"])
    beta = vault.remember("Fact beta", namespace="n")

    moved = _call(
        "update_memory",
        {"memory_id": alpha["id"], "namespace": "archive", "remove_tags": ["draft"], "source": "cleanup"},
    )
    assert not _is_error(moved)
    body = _payload(moved)
    assert body["namespace"] == "archive"
    assert body["tags"] == ["x"]
    assert body["source"] == "cleanup"

    clash = _call("update_memory", {"memory_id": beta["id"], "content": "Fact alpha", "namespace": "archive"})
    assert _is_error(clash)
    assert f"Memory {alpha['id']} in namespace 'archive' already says exactly this" in clash.content[0].text


def test_recall_tool_understands_questions_and_exposes_match(vault):
    vault.remember("Customer ACME prefers deploys on Fridays", namespace="support")
    vault.remember("ACME's billing contact is Jane Doe", namespace="support")

    found = _payload(_call("recall", {"query": "when does ACME prefer to deploy?"}))
    assert found["match_mode"] == "all"
    assert found["hits"][0]["content"] == "Customer ACME prefers deploys on Fridays"
    assert found["hits"][0]["matched_terms"] == ["acme", "prefer", "deploy"]

    partial = _payload(_call("recall", {"query": "ACME's deploy day"}))
    assert partial["match_mode"] == "any" and "note" in partial

    strict = _payload(_call("recall", {"query": "ACME's deploy day", "match": "all"}))
    assert strict["count"] == 0 and "hint" in strict

    bad = _call("recall", {"query": "acme", "match": "fuzzy"})
    assert _is_error(bad)  # rejected by the input schema's enum
