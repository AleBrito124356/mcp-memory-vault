"""Regression tests for the data-integrity guarantees of v0.2.0.

Each test reproduces a bug that v0.1.0 had (offset TTLs purged immediately,
update_memory creating duplicates, tags silently dropped or case-sensitive)
or pins a scale property (index use, no write on read).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_memory_vault import core  # noqa: E402
from mcp_memory_vault.core import MemoryVault, canonical_timestamp  # noqa: E402

Z = "%Y-%m-%dT%H:%M:%SZ"


@pytest.fixture
def vault(tmp_path):
    v = MemoryVault(db_path=tmp_path / "integrity.db")
    yield v
    v.close()


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def test_canonical_timestamp_forms():
    assert canonical_timestamp("2026-07-23T12:00:00Z") == "2026-07-23T12:00:00Z"
    assert canonical_timestamp("2026-07-23T05:00:00-07:00") == "2026-07-23T12:00:00Z"
    assert canonical_timestamp("2026-07-23T14:30:00+02:30") == "2026-07-23T12:00:00Z"
    assert canonical_timestamp("2026-07-23T12:00:00.987654+00:00") == "2026-07-23T12:00:00Z"
    assert canonical_timestamp("2026-07-23T12:00:00") == "2026-07-23T12:00:00Z"  # naive = UTC
    with pytest.raises(ValueError):
        canonical_timestamp("next tuesday")


def test_offset_ttl_import_survives_and_expires_at_the_right_instant(vault, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    expiry = now + timedelta(hours=2)
    pacific = expiry.astimezone(timezone(timedelta(hours=-8))).isoformat()
    assert pacific.endswith("-08:00")

    result = vault.import_memories(
        [{"content": "should live 2 more hours", "namespace": "imp", "expires_at": pacific}]
    )
    assert result["imported"] == 1
    # v0.1.0 stored the raw string and purged it on the very next call.
    listed = vault.list_memories(namespace="imp")
    assert listed["count"] == 1
    assert listed["memories"][0]["expires_at"] == expiry.strftime(Z)

    monkeypatch.setattr(core, "_utc_now", lambda: expiry - timedelta(minutes=1))
    assert vault.list_memories(namespace="imp")["count"] == 1
    monkeypatch.setattr(core, "_utc_now", lambda: expiry + timedelta(seconds=1))
    assert vault.list_memories(namespace="imp")["count"] == 0


def test_import_normalises_created_at_and_skips_already_expired(vault):
    result = vault.import_memories(
        [
            {"content": "offset created", "created_at": "2026-07-23T05:00:00-07:00"},
            {"content": "long gone", "expires_at": "2001-01-01T00:00:00+05:00"},
        ]
    )
    assert result["imported"] == 1
    assert result["expired"] == 1
    memory = vault.list_memories()["memories"][0]
    assert memory["created_at"] == "2026-07-23T12:00:00Z"


# ---------------------------------------------------------------------------
# Dedup is enforced everywhere
# ---------------------------------------------------------------------------

def test_update_memory_refuses_to_create_a_duplicate(vault):
    alpha = vault.remember("Fact alpha", namespace="n")
    beta = vault.remember("Fact beta", namespace="n")
    with pytest.raises(ValueError, match=rf"Memory {alpha['id']} in namespace 'n' already says"):
        vault.update_memory(beta["id"], content="Fact alpha")
    contents = sorted(m["content"] for m in vault.list_memories(namespace="n")["memories"])
    assert contents == ["Fact alpha", "Fact beta"]


def test_namespace_move_refuses_to_create_a_duplicate(vault):
    vault.remember("Shared fact", namespace="a")
    b = vault.remember("Shared fact", namespace="b")
    with pytest.raises(ValueError, match="already says exactly this"):
        vault.update_memory(b["id"], namespace="a")


def test_unique_index_backs_the_guarantee(vault):
    vault.remember("Only once", namespace="x")
    with pytest.raises(sqlite3.IntegrityError):
        vault._conn.execute(
            "INSERT INTO memories (content, namespace, created_at) VALUES (?, ?, ?)",
            ("Only once", "x", "2026-01-01T00:00:00Z"),
        )


def test_duplicates_inside_one_import_batch_are_skipped(vault):
    result = vault.import_memories([{"content": "same"}, {"content": "same"}])
    assert (result["imported"], result["skipped"]) == (1, 1)


def test_import_is_all_or_nothing(vault):
    with pytest.raises(ValueError, match="Item 1 has invalid 'expires_at'"):
        vault.import_memories(
            [{"content": "valid item"}, {"content": "bad", "expires_at": "soon"}]
        )
    assert vault.memory_stats()["total_memories"] == 0


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

def test_remembering_a_duplicate_merges_new_tags(vault):
    first = vault.remember(
        "Customer ACME prefers deploys on Fridays", tags=["customer", "deploy"]
    )
    again = vault.remember(
        "Customer ACME prefers deploys on Fridays", tags=["priority", "CUSTOMER"]
    )
    assert again["deduplicated"] is True
    assert again["id"] == first["id"]
    assert again["merged_tags"] == ["priority"]
    assert again["tags"] == ["customer", "deploy", "priority"]
    assert "added tags: priority" in again["message"]
    assert again["updated_at"] is not None
    assert vault.memory_stats()["total_memories"] == 1


def test_tags_are_case_insensitive_but_keep_their_casing(vault):
    vault.remember("Tagged fact", tags=["Customer", "customer", " VIP "])
    memory = vault.list_memories()["memories"][0]
    assert memory["tags"] == ["Customer", "VIP"]
    assert vault.list_memories(tag="customer")["count"] == 1
    assert vault.list_memories(tag="CUSTOMER")["count"] == 1
    assert vault.recall("tagged", tags=["vip", "CUSTOMER"])["count"] == 1
    assert vault.recall("tagged", tags=["vip", "other"])["count"] == 0


def test_tag_filter_does_not_match_substrings(vault):
    vault.remember("one", tags=["customer-support"])
    vault.remember("two", tags=["customer"])
    assert [m["content"] for m in vault.list_memories(tag="customer")["memories"]] == ["two"]


def test_update_memory_remove_tags_move_and_source(vault):
    stored = vault.remember("ACME contact is Jane", tags=["customer", "Contact"])
    updated = vault.update_memory(
        stored["id"], remove_tags=["contact"], namespace="crm", source="CRM sync"
    )
    assert updated["tags"] == ["customer"]
    assert updated["namespace"] == "crm"
    assert updated["source"] == "CRM sync"
    assert set(updated["updated_fields"]) == {"tags", "namespace", "source"}
    assert updated["updated_at"] is not None
    assert vault.list_memories(namespace="crm")["count"] == 1
    assert vault.list_memories(namespace="default")["count"] == 0

    with pytest.raises(ValueError, match="both add_tags and remove_tags"):
        vault.update_memory(stored["id"], add_tags=["x"], remove_tags=["X"])


def test_control_characters_cannot_forge_tag_matches(vault):
    vault.remember("sneaky", tags=["a\x1fb"])
    memory = vault.list_memories()["memories"][0]
    assert memory["tags"] == ["a b"]
    assert vault.list_memories(tag="a")["count"] == 0


# ---------------------------------------------------------------------------
# Scale: indexes are used, reads do not write
# ---------------------------------------------------------------------------

def _plan(vault, sql, params=()):
    return " | ".join(row[3] for row in vault._conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_hot_queries_use_indexes(vault):
    dedup = _plan(vault, "SELECT * FROM memories WHERE namespace = ? AND content = ?", ("n", "c"))
    assert "idx_memories_namespace_content" in dedup

    purge = _plan(
        vault,
        "SELECT 1 FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ? LIMIT 1",
        ("2026-01-01T00:00:00Z",),
    )
    assert "idx_memories_expires_at" in purge

    listing = _plan(
        vault,
        "SELECT * FROM memories WHERE 1=1 AND namespace = ? ORDER BY created_at DESC, id DESC LIMIT 5",
        ("n",),
    )
    assert "idx_memories_namespace_created" in listing
    assert "TEMP B-TREE" not in listing  # no sort: the index already gives the order


def test_reads_do_not_write_when_nothing_expired(vault):
    vault.remember("a fact", ttl_days=3)
    before = vault._conn.total_changes
    vault.list_memories()
    vault.recall("fact")
    vault.memory_stats()
    assert vault._conn.total_changes == before


def test_reads_are_not_blocked_by_another_writer(vault, tmp_path):
    """Another process holding the write lock must not stall recall/list."""
    vault.remember("Globex staging database resets nightly")
    other = sqlite3.connect(tmp_path / "integrity.db", isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")
        other.execute("UPDATE memories SET source = 'busy elsewhere'")
        vault._conn.execute("PRAGMA busy_timeout = 200")  # fail fast if we'd block
        assert vault.recall("Globex")["count"] == 1
        assert vault.list_memories()["count"] == 1
        assert vault.memory_stats()["total_memories"] == 1
    finally:
        other.execute("ROLLBACK")
        other.close()


def test_stats_report_schema_and_top_tags(vault):
    vault.remember("one", tags=["deploy", "Customer"])
    vault.remember("two", tags=["customer"])
    stats = vault.memory_stats()
    assert stats["schema_version"] == core.SCHEMA_VERSION
    assert stats["top_tags"] == {"Customer": 2, "deploy": 1}
