"""Opening a vault written by v0.1.0 upgrades it in place without data loss."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_memory_vault.core import SCHEMA_VERSION, MemoryVault  # noqa: E402

# The exact DDL that mcp-memory-vault 0.1.0 created (user_version stayed 0).
V010_TABLE = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    namespace TEXT NOT NULL DEFAULT 'default',
    tags TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT
);
"""
V010_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, content='memories', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

V010_ROWS = [
    # content, namespace, tags, source, created_at, expires_at
    ("Customer ACME prefers deploys on Fridays", "support", ["customer", "deploy"],
     "user note", "2026-07-20T10:00:00Z", None),
    ("Imported with offsets", "imp", [], "",
     "2026-07-21T01:00:00-05:00", "2099-01-01T00:00:00+02:00"),
    # update_memory in 0.1.0 could leave exact duplicates behind:
    ("Fact alpha", "n", ["a"], "s1", "2026-07-22T09:00:00Z", "2099-06-01T00:00:00Z"),
    ("Fact alpha", "n", ["A", "b"], "s2", "2026-07-22T08:00:00Z", None),
    ("Mixed case tags", "default", ["Customer", "customer"], "", "2026-07-23T00:00:00Z", None),
]


def _make_v010_db(path: Path, with_fts: bool = True) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V010_TABLE)
    if with_fts:
        conn.executescript(V010_FTS)
    for content, ns, tags, source, created, expires in V010_ROWS:
        conn.execute(
            "INSERT INTO memories (content, namespace, tags, source, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (content, ns, json.dumps(tags), source, created, expires),
        )
    conn.commit()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    conn.close()


def _index_names(vault: MemoryVault) -> set[str]:
    return {
        row[0]
        for row in vault._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"
        )
    }


def test_v010_vault_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    _make_v010_db(path)

    vault = MemoryVault(db_path=path)
    try:
        assert vault.migration == {
            "from_version": 0,
            "to_version": SCHEMA_VERSION,
            "timestamps_normalized": 2,
            "duplicates_merged": 1,
        }
        assert vault.fts_rebuilt is True
        assert vault.schema_version == SCHEMA_VERSION
        assert _index_names(vault) == {
            "idx_memories_namespace_content",
            "idx_memories_expires_at",
            "idx_memories_namespace_created",
            "idx_memories_created",
        }

        memories = {m["id"]: m for m in vault.export_memories()["memories"]}
        assert sorted(memories) == [1, 2, 3, 5]  # row 4 folded into row 3

        acme = memories[1]
        assert acme["tags"] == ["customer", "deploy"]
        assert acme["source"] == "user note"
        assert acme["created_at"] == "2026-07-20T10:00:00Z"

        offsets = memories[2]
        assert offsets["created_at"] == "2026-07-21T06:00:00Z"
        assert offsets["expires_at"] == "2098-12-31T22:00:00Z"

        alpha = memories[3]
        assert alpha["tags"] == ["a", "b"]
        assert alpha["source"] == "s1; s2"
        assert alpha["created_at"] == "2026-07-22T08:00:00Z"  # earliest copy
        assert alpha["expires_at"] is None  # one copy was permanent

        assert memories[5]["tags"] == ["Customer"]

        # Search works on the migrated data, including the new tag filter.
        hits = vault.recall("ACME deploy", tags=["CUSTOMER"])["hits"]
        assert [h["id"] for h in hits] == [1]
        assert vault.list_memories(tag="customer")["count"] == 2

        # And the new guarantees hold from now on.
        again = vault.remember("Fact alpha", namespace="n", tags=["c"])
        assert again["deduplicated"] is True and again["id"] == 3
    finally:
        vault.close()

    reopened = MemoryVault(db_path=path)
    try:
        assert reopened.migration is None
        assert reopened.fts_rebuilt is False  # no full rebuild on a normal start
        assert reopened.memory_stats()["total_memories"] == 4
    finally:
        reopened.close()


def test_v010_vault_written_without_fts5_gets_an_index(tmp_path):
    path = tmp_path / "nofts.db"
    _make_v010_db(path, with_fts=False)
    with MemoryVault(db_path=path) as vault:
        assert vault.search_mode == "fts5"
        assert vault.recall("offsets")["count"] == 1


def test_out_of_sync_index_is_rebuilt_on_open(tmp_path):
    path = tmp_path / "drift.db"
    with MemoryVault(db_path=path) as vault:
        vault.remember("Globex staging database resets nightly")
        vault._conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('delete-all')")
        assert vault.recall("Globex")["count"] == 0  # index lost the row

    with MemoryVault(db_path=path) as vault:
        assert vault.fts_rebuilt is True
        assert vault.recall("Globex")["count"] == 1


def test_rows_written_later_by_v010_are_repaired_on_open(tmp_path):
    """An old install sharing the file writes rows without tag_index."""
    path = tmp_path / "shared.db"
    MemoryVault(db_path=path).close()  # current schema
    conn = sqlite3.connect(path)
    conn.execute(  # exactly what 0.1.0's import_memories would write
        "INSERT INTO memories (content, namespace, tags, source, created_at, expires_at) "
        "VALUES (?, 'default', ?, '', ?, NULL)",
        ("Written by 0.1.0", json.dumps(["Legacy"]), "2026-07-21T01:00:00-05:00"),
    )
    conn.execute(  # untagged, with an offset TTL: only the timestamp gives it away
        "INSERT INTO memories (content, created_at, expires_at) VALUES (?, ?, ?)",
        ("Untagged 0.1.0 row", "2026-07-21T06:00:00Z", "2099-01-01T00:00:00+02:00"),
    )
    conn.commit()
    conn.close()

    with MemoryVault(db_path=path) as vault:
        assert vault.migration is None
        listed = vault.list_memories(tag="legacy")["memories"]
        assert [m["content"] for m in listed] == ["Written by 0.1.0"]
        assert listed[0]["created_at"] == "2026-07-21T06:00:00Z"
        untagged = vault.recall("untagged")["hits"][0]
        assert untagged["expires_at"] == "2098-12-31T22:00:00Z"


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="Upgrade the package"):
        MemoryVault(db_path=path)
