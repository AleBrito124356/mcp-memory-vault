"""Tests for core.MemoryVault — run without mcp installed."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_memory_vault.core import MemoryVault  # noqa: E402


@pytest.fixture
def vault(tmp_path):
    v = MemoryVault(db_path=tmp_path / "memories.db")
    yield v
    v.close()


# ---------------------------------------------------------------------------
# remember + recall
# ---------------------------------------------------------------------------

def test_remember_and_recall_basic(vault):
    stored = vault.remember(
        "Customer ACME prefers deploys on Fridays",
        tags=["customer", "deploy"],
        source="support call",
    )
    assert stored["id"] == 1
    assert stored["deduplicated"] is False
    assert stored["namespace"] == "default"
    assert stored["tags"] == ["customer", "deploy"]
    assert stored["expires_at"] is None

    result = vault.recall("ACME")
    assert result["count"] == 1
    hit = result["hits"][0]
    assert hit["id"] == 1
    assert "ACME" in hit["content"]
    assert "ACME" in hit["snippet"]
    assert hit["tags"] == ["customer", "deploy"]
    assert hit["age"] == "just now"


def test_remember_rejects_empty_content(vault):
    with pytest.raises(ValueError, match="content"):
        vault.remember("   ")


def test_remember_rejects_negative_ttl(vault):
    with pytest.raises(ValueError, match="ttl_days"):
        vault.remember("valid fact", ttl_days=-3)


def test_recall_rejects_empty_query(vault):
    with pytest.raises(ValueError, match="query"):
        vault.recall("  ")


def test_dedup_same_content_and_namespace(vault):
    first = vault.remember("The staging server lives in Frankfurt")
    dup = vault.remember("The staging server lives in Frankfurt")
    assert dup["deduplicated"] is True
    assert dup["id"] == first["id"]
    # Same content in a different namespace is NOT a duplicate.
    other = vault.remember("The staging server lives in Frankfurt", namespace="ops")
    assert other["deduplicated"] is False
    assert other["id"] != first["id"]
    assert vault.memory_stats()["total_memories"] == 2


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------

def test_recall_namespace_filter(vault):
    vault.remember("Project Atlas uses Postgres", namespace="atlas")
    vault.remember("Project Borealis uses Postgres", namespace="borealis")
    result = vault.recall("Postgres", namespace="atlas")
    assert result["count"] == 1
    assert result["hits"][0]["namespace"] == "atlas"


def test_recall_tag_filter(vault):
    vault.remember("Deploy window is Friday afternoon", tags=["deploy", "schedule"])
    vault.remember("Deploy pipeline uses GitHub Actions", tags=["deploy", "ci"])
    result = vault.recall("Deploy", tags=["deploy", "ci"])
    assert result["count"] == 1
    assert "GitHub Actions" in result["hits"][0]["content"]


def test_list_memories_filters_and_order(vault):
    vault.remember("fact one", namespace="a", tags=["x"])
    vault.remember("fact two", namespace="a", tags=["y"])
    vault.remember("fact three", namespace="b", tags=["x"])

    listing = vault.list_memories()
    assert listing["count"] == 3
    # Most recent first (same-second inserts fall back to id DESC).
    assert [m["content"] for m in listing["memories"]] == [
        "fact three",
        "fact two",
        "fact one",
    ]

    by_ns = vault.list_memories(namespace="a")
    assert {m["content"] for m in by_ns["memories"]} == {"fact one", "fact two"}

    by_tag = vault.list_memories(tag="x")
    assert {m["content"] for m in by_tag["memories"]} == {"fact one", "fact three"}

    limited = vault.list_memories(limit=2)
    assert limited["count"] == 2


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------

def test_ttl_sets_expiry_and_purge_removes_expired(vault):
    stored = vault.remember("This will expire", ttl_days=5)
    assert stored["expires_at"] is not None

    keeper = vault.remember("This one stays")
    assert keeper["expires_at"] is None

    # Force expiry by editing the row directly, then trigger a purge via
    # any read operation.
    vault._conn.execute(
        "UPDATE memories SET expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00Z", stored["id"]),
    )
    vault._conn.commit()

    listing = vault.list_memories()
    assert [m["content"] for m in listing["memories"]] == ["This one stays"]
    assert vault.memory_stats()["total_memories"] == 1
    # The expired row is also gone from search.
    assert vault.recall("expire")["count"] == 0


# ---------------------------------------------------------------------------
# forget / update
# ---------------------------------------------------------------------------

def test_forget_deletes_and_reports(vault):
    stored = vault.remember("Temporary note about the beta rollout")
    result = vault.forget(stored["id"])
    assert result["deleted"] is True
    assert "beta rollout" in result["content"]
    assert vault.memory_stats()["total_memories"] == 0
    with pytest.raises(ValueError, match="not found"):
        vault.forget(stored["id"])


def test_update_memory_content_tags_and_ttl(vault):
    stored = vault.remember("ACME contact is Jane", tags=["customer"], ttl_days=10)

    updated = vault.update_memory(
        stored["id"], content="ACME contact is Jane Doe", add_tags=["contact"]
    )
    assert updated["content"] == "ACME contact is Jane Doe"
    assert updated["tags"] == ["customer", "contact"]
    assert "content" in updated["updated_fields"]
    assert "tags" in updated["updated_fields"]
    # ttl_days=-1 (default) leaves the TTL untouched.
    assert updated["expires_at"] == stored["expires_at"]

    # Updated content is searchable; old content is not required to match.
    assert vault.recall("Doe")["count"] == 1

    # ttl_days=0 removes the TTL.
    cleared = vault.update_memory(stored["id"], ttl_days=0)
    assert cleared["expires_at"] is None

    # Positive ttl_days sets a fresh expiry.
    renewed = vault.update_memory(stored["id"], ttl_days=3)
    assert renewed["expires_at"] is not None


def test_update_memory_errors(vault):
    with pytest.raises(ValueError, match="not found"):
        vault.update_memory(999, content="nope")
    stored = vault.remember("something")
    with pytest.raises(ValueError, match="Nothing to update"):
        vault.update_memory(stored["id"])
    with pytest.raises(ValueError, match="ttl_days"):
        vault.update_memory(stored["id"], ttl_days=-2)


# ---------------------------------------------------------------------------
# export / import
# ---------------------------------------------------------------------------

def test_export_import_round_trip(vault, tmp_path):
    vault.remember("Fact A", namespace="proj", tags=["t1"], source="s1")
    vault.remember("Fact B", namespace="proj", tags=["t2"], ttl_days=30)
    vault.remember("Fact C", namespace="other")

    export = vault.export_memories()
    assert export["count"] == 3

    target = MemoryVault(db_path=tmp_path / "other.db")
    try:
        result = target.import_memories(export["memories"])
        assert result["imported"] == 3
        assert result["skipped"] == 0

        re_export = target.export_memories()
        original = {
            (m["content"], m["namespace"], tuple(m["tags"]), m["created_at"], m["expires_at"])
            for m in export["memories"]
        }
        round_tripped = {
            (m["content"], m["namespace"], tuple(m["tags"]), m["created_at"], m["expires_at"])
            for m in re_export["memories"]
        }
        assert original == round_tripped

        # Importing again skips everything as duplicates.
        again = target.import_memories(export["memories"])
        assert again["imported"] == 0
        assert again["skipped"] == 3
    finally:
        target.close()


def test_export_namespace_filter(vault):
    vault.remember("Fact A", namespace="proj")
    vault.remember("Fact B", namespace="other")
    export = vault.export_memories(namespace="proj")
    assert export["count"] == 1
    assert export["memories"][0]["content"] == "Fact A"


def test_import_validation(vault):
    with pytest.raises(ValueError, match="empty 'content'"):
        vault.import_memories([{"namespace": "x"}])
    with pytest.raises(ValueError, match="not a dict"):
        vault.import_memories([["not", "a", "dict"]])
    with pytest.raises(ValueError, match="created_at"):
        vault.import_memories([{"content": "ok", "created_at": "not-a-date"}])


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def test_memory_stats(vault, tmp_path):
    vault.remember("one", namespace="a")
    vault.remember("two", namespace="a", ttl_days=7)
    vault.remember("three", namespace="b")

    stats = vault.memory_stats()
    assert stats["total_memories"] == 3
    assert stats["by_namespace"] == {"a": 2, "b": 1}
    assert stats["with_active_ttl"] == 1
    assert stats["db_size_bytes"] > 0
    assert stats["db_path"] == str(tmp_path / "memories.db")
    assert stats["search_mode"] in ("fts5", "like")


# ---------------------------------------------------------------------------
# LIKE fallback (forced) and multi-word search
# ---------------------------------------------------------------------------

def _seed_ten_facts(v):
    facts = [
        ("Customer ACME prefers deploys on Fridays", ["customer", "deploy"]),
        ("Globex staging database resets nightly", ["ops"]),
        ("Initech invoices are due net-30", ["billing"]),
        ("Umbrella wants weekly status emails", ["customer"]),
        ("Stark Industries uses Kubernetes on-prem", ["infra"]),
        ("Wayne Enterprises signed the annual contract", ["sales"]),
        ("Hooli rate limits their public API at 100 rps", ["infra"]),
        ("Pied Piper compression benchmarks run on Tuesdays", ["ops"]),
        ("Vandelay imports run through the EU region", ["ops"]),
        ("Wonka factory tours are booked via the portal", ["misc"]),
    ]
    for content, tags in facts:
        v.remember(content, tags=tags)


def test_multiword_search_finds_right_fact_among_ten(vault):
    _seed_ten_facts(vault)
    result = vault.recall("ACME deploy")
    assert result["search_mode"] == "fts5"
    assert result["count"] == 1
    assert result["hits"][0]["content"] == "Customer ACME prefers deploys on Fridays"


def test_like_fallback(vault, monkeypatch):
    _seed_ten_facts(vault)
    monkeypatch.setattr(vault, "fts5_available", False)

    assert vault.memory_stats()["search_mode"] == "like"

    # Multi-word AND semantics still find the single right fact.
    result = vault.recall("ACME deploy")
    assert result["search_mode"] == "like"
    assert result["count"] == 1
    hit = result["hits"][0]
    assert hit["content"] == "Customer ACME prefers deploys on Fridays"
    # Fallback snippet is the (possibly truncated) content itself.
    assert hit["snippet"].startswith("Customer ACME")

    # Namespace and tag filters work in fallback mode too.
    assert vault.recall("deploys", tags=["customer"])["count"] == 1
    assert vault.recall("deploys", namespace="nope")["count"] == 0

    # LIKE wildcards in the query are escaped, not interpreted.
    assert vault.recall("100%")["count"] == 0
    assert vault.recall("100")["count"] == 1
