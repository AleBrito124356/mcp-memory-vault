"""Core logic for mcp-memory-vault.

A persistent memory store for AI agents built entirely on the Python
standard library: namespaced facts with tags, optional TTL expiry, and
full-text search backed by SQLite FTS5. When the local SQLite build was
compiled without FTS5, the vault transparently falls back to a term-wise
LIKE search (AND semantics) and reports ``"search_mode": "like"`` in
``memory_stats()``.

Storage lives in ``~/.mcp-memory-vault/memories.db`` by default; override
with the ``MEMORY_VAULT_DB`` environment variable or by passing an
explicit ``db_path`` to :class:`MemoryVault`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_SNIPPET_TOKENS = 12
_FALLBACK_SNIPPET_CHARS = 160


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (second precision, Z suffix)."""
    return _utc_now().strftime(_TIME_FORMAT)


def _parse_iso(timestamp: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating both 'Z' and offset forms."""
    try:
        return datetime.strptime(timestamp, _TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed


def _human_age(created_at: str) -> str:
    """Render a created_at timestamp as a readable age like '3 days ago'."""
    try:
        created = _parse_iso(created_at)
    except (ValueError, TypeError):
        return "unknown"
    seconds = max(0, int((_utc_now() - created).total_seconds()))
    if seconds < 60:
        return "just now"
    for unit_seconds, singular in (
        (365 * 24 * 3600, "year"),
        (30 * 24 * 3600, "month"),
        (7 * 24 * 3600, "week"),
        (24 * 3600, "day"),
        (3600, "hour"),
        (60, "minute"),
    ):
        if seconds >= unit_seconds:
            count = seconds // unit_seconds
            noun = singular if count == 1 else singular + "s"
            return f"{count} {noun} ago"
    return "just now"


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _normalize_tags(tags: list[str] | None) -> list[str]:
    """Strip, drop empties, and de-duplicate tags while preserving order."""
    if not tags:
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise ValueError(
                f"Tags must be strings — got {type(tag).__name__} ({tag!r})."
            )
        tag = tag.strip()
        if tag and tag not in seen:
            seen.add(tag)
            cleaned.append(tag)
    return cleaned


def _truncate(text: str, limit: int = 80) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so user terms match literally."""
    return (
        term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _fts_match_expression(query: str) -> str:
    """Sanitize a raw query into an FTS5 MATCH expression.

    Each whitespace-separated term is double-quoted (so FTS5 operators in
    user input are treated literally) and terms are joined with AND
    semantics (FTS5's implicit conjunction).
    """
    terms = [t for t in query.split() if t]
    return " ".join('"' + term.replace('"', '""') + '"' for term in terms)


# ---------------------------------------------------------------------------
# MemoryVault
# ---------------------------------------------------------------------------

class MemoryVault:
    """SQLite-backed persistent memory with namespaces, tags, TTL and FTS."""

    def __init__(self, db_path: str | os.PathLike | None = None):
        if db_path is None:
            db_path = os.environ.get("MEMORY_VAULT_DB") or (
                Path.home() / ".mcp-memory-vault" / "memories.db"
            )
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.fts5_available = True
        self._init_schema()

    # -- schema -------------------------------------------------------------

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                namespace TEXT NOT NULL DEFAULT 'default',
                tags TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                expires_at TEXT
            )
            """
        )
        try:
            self._conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    content,
                    content='memories',
                    content_rowid='id',
                    tokenize='porter unicode61'
                )
                """
            )
            self._conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS memories_ai
                AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, content)
                    VALUES (new.id, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS memories_ad
                AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content)
                    VALUES ('delete', old.id, old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS memories_au
                AFTER UPDATE OF content ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, content)
                    VALUES ('delete', old.id, old.content);
                    INSERT INTO memories_fts(rowid, content)
                    VALUES (new.id, new.content);
                END;
                """
            )
            # Keep the index consistent with any rows written by a previous
            # process (e.g. one that ran without FTS5 support).
            self._conn.execute(
                "INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')"
            )
            self.fts5_available = True
        except sqlite3.OperationalError:
            self.fts5_available = False
        self._conn.commit()

    # -- internals ----------------------------------------------------------

    def _purge_expired(self) -> None:
        """Delete expired memories. Runs before every read/write operation."""
        self._conn.execute(
            "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (_now_iso(),),
        )
        self._conn.commit()

    def _row_to_dict(self, row: sqlite3.Row, include_age: bool = True) -> dict:
        memory = {
            "id": row["id"],
            "content": row["content"],
            "namespace": row["namespace"],
            "tags": json.loads(row["tags"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }
        if include_age:
            memory["age"] = _human_age(row["created_at"])
        return memory

    def _get_row(self, memory_id: int) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            raise ValueError(
                f"Memory {memory_id} not found — it may have expired or been "
                "forgotten. Use list_memories or recall to find valid ids."
            )
        return row

    @property
    def search_mode(self) -> str:
        return "fts5" if self.fts5_available else "like"

    # -- tools --------------------------------------------------------------

    def remember(
        self,
        content: str,
        namespace: str = "default",
        tags: list[str] | None = None,
        ttl_days: int = 0,
        source: str = "",
    ) -> dict:
        """Store a fact. Returns the stored memory (deduplicated if exact match)."""
        self._purge_expired()
        content = (content or "").strip()
        if not content:
            raise ValueError(
                "content must not be empty — pass the fact to remember as a "
                "non-empty string."
            )
        if ttl_days < 0:
            raise ValueError(
                "ttl_days must be >= 0 (0 means the memory never expires)."
            )
        namespace = (namespace or "").strip() or "default"
        clean_tags = _normalize_tags(tags)

        existing = self._conn.execute(
            "SELECT * FROM memories WHERE content = ? AND namespace = ?",
            (content, namespace),
        ).fetchone()
        if existing is not None:
            result = self._row_to_dict(existing)
            result["deduplicated"] = True
            result["message"] = (
                f"Identical memory already exists in namespace "
                f"'{namespace}' (id {existing['id']}) — nothing stored."
            )
            return result

        expires_at = None
        if ttl_days > 0:
            expires_at = (_utc_now() + timedelta(days=ttl_days)).strftime(_TIME_FORMAT)

        cursor = self._conn.execute(
            """
            INSERT INTO memories (content, namespace, tags, source, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                content,
                namespace,
                json.dumps(clean_tags, ensure_ascii=False),
                source or "",
                _now_iso(),
                expires_at,
            ),
        )
        self._conn.commit()
        row = self._get_row(cursor.lastrowid)
        result = self._row_to_dict(row)
        result["deduplicated"] = False
        result["message"] = f"Stored memory {row['id']} in namespace '{namespace}'."
        return result

    def recall(
        self,
        query: str,
        namespace: str = "",
        tags: list[str] | None = None,
        limit: int = 8,
    ) -> dict:
        """Full-text search over stored memories, best matches first."""
        self._purge_expired()
        if not (query or "").strip():
            raise ValueError(
                "query must not be empty — pass one or more search terms, or "
                "use list_memories to browse."
            )
        if limit < 1:
            limit = 1
        namespace = (namespace or "").strip()
        required_tags = _normalize_tags(tags)

        if self.fts5_available:
            rows = self._recall_fts(query, namespace)
        else:
            rows = self._recall_like(query, namespace)

        hits = []
        for row, snippet in rows:
            memory = self._row_to_dict(row)
            if required_tags and not set(required_tags).issubset(memory["tags"]):
                continue
            memory["snippet"] = snippet
            hits.append(memory)
            if len(hits) >= limit:
                break

        return {
            "query": query,
            "search_mode": self.search_mode,
            "count": len(hits),
            "hits": hits,
        }

    def _recall_fts(self, query: str, namespace: str) -> list[tuple[sqlite3.Row, str]]:
        match_expr = _fts_match_expression(query)
        sql = f"""
            SELECT m.*,
                   snippet(memories_fts, 0, '[', ']', ' … ', {_SNIPPET_TOKENS}) AS snip,
                   bm25(memories_fts) AS score
            FROM memories_fts
            JOIN memories AS m ON m.id = memories_fts.rowid
            WHERE memories_fts MATCH ?
        """
        params: list = [match_expr]
        if namespace:
            sql += " AND m.namespace = ?"
            params.append(namespace)
        sql += " ORDER BY score ASC, m.created_at DESC, m.id DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [(row, row["snip"]) for row in rows]

    def _recall_like(self, query: str, namespace: str) -> list[tuple[sqlite3.Row, str]]:
        terms = [t for t in query.split() if t]
        sql = "SELECT * FROM memories WHERE 1=1"
        params: list = []
        for term in terms:
            sql += " AND content LIKE ? ESCAPE '\\'"
            params.append("%" + _escape_like(term) + "%")
        if namespace:
            sql += " AND namespace = ?"
            params.append(namespace)
        sql += " ORDER BY created_at DESC, id DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [(row, _truncate(row["content"], _FALLBACK_SNIPPET_CHARS)) for row in rows]

    def forget(self, memory_id: int) -> dict:
        """Delete a memory by id, confirming what was removed."""
        self._purge_expired()
        row = self._get_row(memory_id)
        self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        self._conn.commit()
        return {
            "deleted": True,
            "id": memory_id,
            "namespace": row["namespace"],
            "content": _truncate(row["content"]),
            "message": f"Forgot memory {memory_id}: {_truncate(row['content'])}",
        }

    def list_memories(
        self, namespace: str = "", tag: str = "", limit: int = 20
    ) -> dict:
        """List memories, most recent first, optionally filtered."""
        self._purge_expired()
        if limit < 1:
            limit = 1
        namespace = (namespace or "").strip()
        tag = (tag or "").strip()

        sql = "SELECT * FROM memories"
        params: list = []
        if namespace:
            sql += " WHERE namespace = ?"
            params.append(namespace)
        sql += " ORDER BY created_at DESC, id DESC"
        rows = self._conn.execute(sql, params).fetchall()

        memories = []
        for row in rows:
            memory = self._row_to_dict(row)
            if tag and tag not in memory["tags"]:
                continue
            memories.append(memory)
            if len(memories) >= limit:
                break
        return {"count": len(memories), "memories": memories}

    def update_memory(
        self,
        memory_id: int,
        content: str = "",
        add_tags: list[str] | None = None,
        ttl_days: int = -1,
    ) -> dict:
        """Update a memory's content, tags, and/or TTL.

        ttl_days: -1 leaves the TTL untouched, 0 removes it (memory becomes
        permanent), any positive value sets a new expiry from now.
        """
        self._purge_expired()
        if ttl_days < -1:
            raise ValueError(
                "ttl_days must be -1 (leave unchanged), 0 (remove TTL), or a "
                "positive number of days."
            )
        content = (content or "").strip()
        new_tags = _normalize_tags(add_tags)
        if not content and not new_tags and ttl_days == -1:
            raise ValueError(
                "Nothing to update — pass content, add_tags, and/or ttl_days."
            )

        row = self._get_row(memory_id)
        updated_fields: list[str] = []

        if content and content != row["content"]:
            self._conn.execute(
                "UPDATE memories SET content = ? WHERE id = ?", (content, memory_id)
            )
            updated_fields.append("content")

        if new_tags:
            merged = json.loads(row["tags"])
            for tag in new_tags:
                if tag not in merged:
                    merged.append(tag)
            if merged != json.loads(row["tags"]):
                self._conn.execute(
                    "UPDATE memories SET tags = ? WHERE id = ?",
                    (json.dumps(merged, ensure_ascii=False), memory_id),
                )
                updated_fields.append("tags")

        if ttl_days == 0:
            if row["expires_at"] is not None:
                self._conn.execute(
                    "UPDATE memories SET expires_at = NULL WHERE id = ?", (memory_id,)
                )
                updated_fields.append("expires_at")
        elif ttl_days > 0:
            expires_at = (_utc_now() + timedelta(days=ttl_days)).strftime(_TIME_FORMAT)
            self._conn.execute(
                "UPDATE memories SET expires_at = ? WHERE id = ?",
                (expires_at, memory_id),
            )
            updated_fields.append("expires_at")

        self._conn.commit()
        result = self._row_to_dict(self._get_row(memory_id))
        result["updated_fields"] = updated_fields
        result["message"] = (
            f"Updated memory {memory_id} ({', '.join(updated_fields)})."
            if updated_fields
            else f"Memory {memory_id} already matched the requested state."
        )
        return result

    def memory_stats(self) -> dict:
        """Vault statistics: totals, namespaces, TTL usage, size, search mode."""
        self._purge_expired()
        total = self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        by_namespace = {
            row["namespace"]: row["n"]
            for row in self._conn.execute(
                "SELECT namespace, COUNT(*) AS n FROM memories "
                "GROUP BY namespace ORDER BY n DESC, namespace"
            )
        }
        with_ttl = self._conn.execute(
            "SELECT COUNT(*) FROM memories WHERE expires_at IS NOT NULL"
        ).fetchone()[0]
        try:
            db_size = self.db_path.stat().st_size
        except OSError:
            db_size = 0
        return {
            "total_memories": total,
            "by_namespace": by_namespace,
            "with_active_ttl": with_ttl,
            "db_size_bytes": db_size,
            "db_path": str(self.db_path),
            "search_mode": self.search_mode,
        }

    def export_memories(self, namespace: str = "") -> dict:
        """Export memories as plain JSON-serializable dicts (for backup)."""
        self._purge_expired()
        namespace = (namespace or "").strip()
        sql = "SELECT * FROM memories"
        params: list = []
        if namespace:
            sql += " WHERE namespace = ?"
            params.append(namespace)
        sql += " ORDER BY id"
        rows = self._conn.execute(sql, params).fetchall()
        memories = [self._row_to_dict(row, include_age=False) for row in rows]
        return {
            "count": len(memories),
            "namespace": namespace or "(all)",
            "memories": memories,
        }

    def import_memories(self, memories: list[dict]) -> dict:
        """Import memories previously produced by export_memories.

        Validates every item; exact duplicates (same content + namespace)
        are skipped. Returns counts of imported and skipped items.
        """
        self._purge_expired()
        if not isinstance(memories, list):
            raise ValueError(
                "memories must be a list of dicts as produced by "
                "export_memories (the 'memories' array)."
            )
        imported = 0
        skipped = 0
        for index, item in enumerate(memories):
            if not isinstance(item, dict):
                raise ValueError(
                    f"Item {index} is not a dict — pass the 'memories' array "
                    "from export_memories."
                )
            content = str(item.get("content") or "").strip()
            if not content:
                raise ValueError(
                    f"Item {index} has empty 'content' — every imported "
                    "memory needs a non-empty content string."
                )
            namespace = str(item.get("namespace") or "").strip() or "default"
            raw_tags = item.get("tags") or []
            if isinstance(raw_tags, str):
                try:
                    raw_tags = json.loads(raw_tags)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Item {index} has invalid 'tags' — expected a list "
                        "of strings or its JSON encoding."
                    ) from exc
            if not isinstance(raw_tags, list):
                raise ValueError(
                    f"Item {index} has invalid 'tags' — expected a list of strings."
                )
            clean_tags = _normalize_tags(raw_tags)
            source = str(item.get("source") or "")
            created_at = str(item.get("created_at") or "").strip() or _now_iso()
            try:
                _parse_iso(created_at)
            except ValueError as exc:
                raise ValueError(
                    f"Item {index} has invalid 'created_at' ({created_at!r}) "
                    "— expected ISO-8601, e.g. 2026-07-23T12:00:00Z."
                ) from exc
            expires_at = item.get("expires_at")
            if expires_at is not None:
                expires_at = str(expires_at).strip() or None
            if expires_at is not None:
                try:
                    _parse_iso(expires_at)
                except ValueError as exc:
                    raise ValueError(
                        f"Item {index} has invalid 'expires_at' "
                        f"({expires_at!r}) — expected ISO-8601 or null."
                    ) from exc

            duplicate = self._conn.execute(
                "SELECT id FROM memories WHERE content = ? AND namespace = ?",
                (content, namespace),
            ).fetchone()
            if duplicate is not None:
                skipped += 1
                continue

            self._conn.execute(
                """
                INSERT INTO memories (content, namespace, tags, source, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    content,
                    namespace,
                    json.dumps(clean_tags, ensure_ascii=False),
                    source,
                    created_at,
                    expires_at,
                ),
            )
            imported += 1
        self._conn.commit()
        return {
            "imported": imported,
            "skipped": skipped,
            "total_received": len(memories),
            "message": f"Imported {imported} memories, skipped {skipped} duplicates.",
        }

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()
