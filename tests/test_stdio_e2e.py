"""End-to-end test: spawn the real server and speak raw JSON-RPC over stdio.

No MCP client library is involved on the test side, so this checks exactly
what Claude Desktop / Claude Code see, whatever SDK version is installed.
Fully offline: the server only touches a temporary SQLite file.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("mcp")

REPO = Path(__file__).resolve().parents[1]
TIMEOUT = 30


class StdioServer:
    def __init__(self, db_path: Path):
        env = dict(os.environ, MEMORY_VAULT_DB=str(db_path), PYTHONPATH=str(REPO))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_memory_vault.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=REPO,
        )
        self._lines: queue.Queue[bytes] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._next_id = 0

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self._lines.put(line)

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        while True:
            try:
                line = self._lines.get(timeout=TIMEOUT)
            except queue.Empty:
                raise AssertionError(f"no response to {method} within {TIMEOUT}s") from None
            reply = json.loads(line)
            if reply.get("id") == self._next_id:
                return reply

    def call_tool(self, name: str, arguments: dict) -> dict:
        reply = self.request("tools/call", {"name": name, "arguments": arguments})
        assert "result" in reply, reply
        return reply["result"]

    def _send(self, message: dict) -> None:
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def close(self) -> int:
        self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=TIMEOUT)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()


@pytest.fixture
def stdio_server(tmp_path):
    srv = StdioServer(tmp_path / "e2e.db")
    yield srv
    if srv.proc.poll() is None:
        srv.close()


def test_stdio_json_rpc_round_trip(stdio_server, tmp_path):
    init = stdio_server.request(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest-raw-client", "version": "0"},
        },
    )
    result = init["result"]
    assert result["serverInfo"]["name"] == "mcp-memory-vault"
    assert "REMEMBER" in result["instructions"] and "RECALL" in result["instructions"]
    stdio_server.notify("notifications/initialized")

    tools = stdio_server.request("tools/list")["result"]["tools"]
    assert len(tools) == 8
    assert {t["name"] for t in tools} >= {"remember", "recall", "forget"}

    stored = stdio_server.call_tool(
        "remember",
        {
            "content": "Customer ACME prefers deploys on Fridays",
            "namespace": "support",
            "tags": ["customer", "deploy"],
        },
    )
    assert not stored.get("isError")
    memory = json.loads(stored["content"][0]["text"])
    assert memory["deduplicated"] is False

    found = stdio_server.call_tool("recall", {"query": "ACME deploy"})
    hits = json.loads(found["content"][0]["text"])["hits"]
    assert hits[0]["id"] == memory["id"]
    assert hits[0]["snippet"] == "Customer [ACME] prefers [deploys] on Fridays"

    missing = stdio_server.call_tool("forget", {"memory_id": 999})
    assert missing["isError"] is True
    assert "Memory 999 not found" in missing["content"][0]["text"]
    assert "list_memories" in missing["content"][0]["text"]

    assert stdio_server.close() == 0
    # The data landed in the vault named by MEMORY_VAULT_DB.
    assert (tmp_path / "e2e.db").exists()
