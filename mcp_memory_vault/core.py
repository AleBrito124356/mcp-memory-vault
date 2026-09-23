"""Core logic for mcp-memory-vault.

A persistent memory store for AI agents built entirely on the Python
standard library: namespaced facts with tags, optional TTL expiry, and
full-text search backed by SQLite FTS5. When the local SQLite build was
compiled without FTS5, the vault transparently falls back to a term-wise
LIKE search and reports ``"search_mode": "like"`` in ``memory_stats()``.

Storage lives in ``~/.mcp-memory-vault/memories.db`` by default; override
with the ``MEMORY_VAULT_DB`` environment variable or by passing an
explicit ``db_path`` to :class:`MemoryVault`.

Data model guarantees:

* Every timestamp is stored as canonical UTC (``2026-07-23T12:00:00Z``), so
  TTL expiry can be decided by plain string comparison in an index. Inputs
  with offsets (``...-08:00``) are converted, never stored raw.
* ``(namespace, content)`` is unique, enforced by a UNIQUE index: no code
  path (remember, update, import, a second process) can create duplicates.
* Tags compare case-insensitively ("Customer" == "customer") and keep the
  casing they were first written with.
* The schema is versioned with ``PRAGMA user_version`` and upgraded in place
  (in one transaction) when an older vault is opened.

Thread safety: one :class:`MemoryVault` owns one SQLite connection, and every
public method runs under a re-entrant lock, so a single instance can be shared
by the worker threads an MCP SDK uses to run synchronous tools. Separate
processes (two agents, or an agent plus the CLI) coordinate through SQLite's
own locking, with a busy timeout instead of failing on the first contention.
"""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: Current on-disk schema. 0/1 = v0.1.0 (no indexes, raw timestamps).
SCHEMA_VERSION = 2

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_SNIPPET_TOKENS = 12
_FALLBACK_SNIPPET_CHARS = 160
_BUSY_TIMEOUT_MS = 10_000
_TAG_SEP = "\x1f"  # unit separator: delimits tags in the tag_index column
_TOP_TAGS = 15


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (second precision, Z suffix)."""
    return _utc_now().strftime(_TIME_FORMAT)


def _parse_iso(timestamp: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating 'Z', offsets and naive (UTC)."""
    if not isinstance(timestamp, str):
        raise ValueError(f"expected an ISO-8601 string, got {type(timestamp).__name__}")
    timestamp = timestamp.strip()
    try:
        return datetime.strptime(timestamp, _TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00").replace("z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed


def canonical_timestamp(timestamp: str) -> str:
    """Normalise any accepted ISO-8601 form to canonical UTC ``...Z``.

    ``2026-09-23T09:45:00-08:00`` becomes ``2026-09-23T17:45:00Z``. Raises
    ``ValueError`` for anything that is not a valid ISO-8601 timestamp.
    """
    try:
        return _parse_iso(timestamp).astimezone(timezone.utc).strftime(_TIME_FORMAT)
    except (OverflowError, TypeError) as exc:
        raise ValueError(str(exc)) from exc


def _expiry_from_now(days: int) -> str:
    return (_utc_now() + timedelta(days=days)).strftime(_TIME_FORMAT)


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
# Tag helpers
# ---------------------------------------------------------------------------

def _clean_tag(tag: object) -> str:
    if not isinstance(tag, str):
        raise ValueError(
            f"Tags must be strings — got {type(tag).__name__} ({tag!r})."
        )
    # Control characters (including the tag-index separator) become spaces.
    tag = "".join(" " if ord(ch) < 32 or ord(ch) == 127 else ch for ch in tag)
    return " ".join(tag.split())


def _normalize_tags(tags: list[str] | str | None) -> list[str]:
    """Clean tags and drop empties and case-insensitive duplicates.

    The first spelling of a tag wins, so ``["Customer", "customer"]`` becomes
    ``["Customer"]``. A bare string is treated as a single tag.
    """
    if not tags:
        return []
    if isinstance(tags, str):
        tags = [tags]
    seen: set[str] = set()
    cleaned: list[str] = []
    for tag in tags:
        tag = _clean_tag(tag)
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            cleaned.append(tag)
    return cleaned


def _merge_tags(current: list[str], extra: list[str]) -> tuple[list[str], list[str]]:
    """Append the tags from ``extra`` that ``current`` lacks (case-insensitive)."""
    have = {t.casefold() for t in current}
    added = [t for t in extra if t.casefold() not in have]
    return current + added, added


def _tag_index(tags: list[str]) -> str:
    """Case-folded, separator-delimited tag list used for SQL filtering."""
    if not tags:
        return ""
    return _TAG_SEP + _TAG_SEP.join(t.casefold() for t in tags) + _TAG_SEP


def _tag_needle(tag: str) -> str:
    return _TAG_SEP + tag.casefold() + _TAG_SEP


def _load_tags(raw: str | None) -> list[str]:
    """Decode the JSON tags column, tolerating legacy/garbled values."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _normalize_tags([raw])
    if isinstance(value, str):
        return _normalize_tags([value])
    if not isinstance(value, list):
        return []
    return _normalize_tags([t for t in value if isinstance(t, str)])


def _dump_tags(tags: list[str]) -> str:
    return json.dumps(tags, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

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
    """Sanitize a raw query into an FTS5 MATCH expression (AND of quoted terms)."""
    terms = [t for t in query.split() if t]
    return " ".join('"' + term.replace('"', '""') + '"' for term in terms)


def fts5_supported(conn: sqlite3.Connection) -> bool:
    """True when this SQLite build can create FTS5 tables."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._mv_fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._mv_fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _synchronized(method):
    """Run a MemoryVault method while holding the instance lock.

    ``sqlite3`` connections are not safe to use from several threads at once:
    interleaved statements corrupt cursor state and lose writes. The lock is
    re-entrant so public methods may call each other.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_MEMORIES = """
    CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        namespace TEXT NOT NULL DEFAULT 'default',
        tags TEXT NOT NULL DEFAULT '[]',
        source TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        expires_at TEXT,
        tag_index TEXT NOT NULL DEFAULT '',
        updated_at TEXT
    )
"""

_INDEXES = (
    # Dedup lookups, and the guarantee that duplicates cannot exist.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_namespace_content "
    "ON memories(namespace, content)",
    # The expiry purge that runs before every operation.
    "CREATE INDEX IF NOT EXISTS idx_memories_expires_at "
    "ON memories(expires_at) WHERE expires_at IS NOT NULL",
    # Most-recent-first listing, with and without a namespace.
    "CREATE INDEX IF NOT EXISTS idx_memories_namespace_created "
    "ON memories(namespace, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at)",
)

_FTS_TABLE = """
    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        content,
        content='memories',
        content_rowid='id',
        tokenize='porter unicode61'
    )
"""

_FTS_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS memories_ai
    AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_ad
    AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memories_au
    AFTER UPDATE OF content ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
        INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
    END
    """,
)


# ---------------------------------------------------------------------------
# MemoryVault
# ---------------------------------------------------------------------------

class MemoryVault:
    """SQLite-backed persistent memory with namespaces, tags, TTL and FTS.

    Attributes set when the vault is opened:

    ``fts5_available``
        Whether ranked FTS5 search is used (else the LIKE fallback).
    ``migration``
        ``None`` when the schema was already current, otherwise a report of
        the in-place upgrade (``from_version``, ``to_version``,
        ``timestamps_normalized``, ``duplicates_merged``).
    ``fts_rebuilt``
        Whether the full-text index had to be rebuilt on open (only after a
        migration, or when it disagrees with the table).
    """

    def __init__(self, db_path: str | os.PathLike | None = None):
        if db_path is None:
            db_path = os.environ.get("MEMORY_VAULT_DB") or (
                Path.home() / ".mcp-memory-vault" / "memories.db"
            )
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # isolation_level=None: autocommit, with explicit BEGIN IMMEDIATE for
        # every write so check-then-write sequences are atomic across processes.
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.fts5_available = False
        self.fts_rebuilt = False
        self.migration: dict | None = None
        try:
            self._open()
        except BaseException:
            self._conn.close()
            raise

    # -- transactions -------------------------------------------------------

    @contextmanager
    def _write(self):
        """Run the block in one IMMEDIATE transaction (nesting joins it)."""
        if self._conn.in_transaction:
            yield
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # -- schema -------------------------------------------------------------

    def _open(self) -> None:
        fts5 = fts5_supported(self._conn)
        with self._write():
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self.db_path} uses schema version {version}, but this "
                    f"mcp-memory-vault only understands up to {SCHEMA_VERSION}. "
                    "Upgrade the package to open it."
                )
            if version < SCHEMA_VERSION:
                self.migration = self._migrate(version)
            if fts5:
                self._ensure_fts(force_rebuild=self.migration is not None)
        self.fts5_available = fts5

    def _migrate(self, from_version: int) -> dict:
        """Upgrade the schema in place. Runs inside the open transaction."""
        report = {
            "from_version": from_version,
            "to_version": SCHEMA_VERSION,
            "timestamps_normalized": 0,
            "duplicates_merged": 0,
        }
        # v0 -> v1: the original table (a no-op for vaults made by v0.1.0).
        self._conn.execute(_CREATE_MEMORIES)
        # v1 -> v2: tag index, updated_at, canonical data, indexes.
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(memories)")}
        if "tag_index" not in columns:
            self._conn.execute(
                "ALTER TABLE memories ADD COLUMN tag_index TEXT NOT NULL DEFAULT ''"
            )
        if "updated_at" not in columns:
            self._conn.execute("ALTER TABLE memories ADD COLUMN updated_at TEXT")
        report["timestamps_normalized"] = self._normalize_rows()
        report["duplicates_merged"] = self._merge_duplicates()
        for statement in _INDEXES:
            self._conn.execute(statement)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return report

    def _normalize_rows(self) -> int:
        """Canonicalise timestamps and tags of every row; return #timestamps fixed."""
        fixed = 0
        rows = self._conn.execute(
            "SELECT id, tags, tag_index, created_at, expires_at, updated_at FROM memories"
        ).fetchall()
        for row in rows:
            changes: dict[str, object] = {}
            for column in ("created_at", "expires_at", "updated_at"):
                value = row[column]
                if value is None:
                    continue
                try:
                    canonical = canonical_timestamp(value)
                except ValueError:
                    continue  # unparseable legacy value: leave it untouched
                if canonical != value:
                    changes[column] = canonical
                    fixed += 1
            tags = _load_tags(row["tags"])
            if _dump_tags(tags) != row["tags"]:
                changes["tags"] = _dump_tags(tags)
            if _tag_index(tags) != row["tag_index"]:
                changes["tag_index"] = _tag_index(tags)
            if changes:
                assignments = ", ".join(f"{column} = ?" for column in changes)
                self._conn.execute(
                    f"UPDATE memories SET {assignments} WHERE id = ?",
                    (*changes.values(), row["id"]),
                )
        return fixed

    def _merge_duplicates(self) -> int:
        """Fold exact duplicates (same namespace + content) into the oldest row.

        Tags are unioned, the earliest created_at is kept, the memory stays
        permanent if any copy was, and distinct sources are joined. Returns
        the number of rows removed.
        """
        removed = 0
        groups = self._conn.execute(
            "SELECT namespace, content FROM memories "
            "GROUP BY namespace, content HAVING COUNT(*) > 1"
        ).fetchall()
        for group in groups:
            rows = self._conn.execute(
                "SELECT * FROM memories WHERE namespace = ? AND content = ? ORDER BY id",
                (group["namespace"], group["content"]),
            ).fetchall()
            keep = rows[0]
            tags: list[str] = []
            sources: list[str] = []
            for row in rows:
                tags, _ = _merge_tags(tags, _load_tags(row["tags"]))
                if row["source"] and row["source"] not in sources:
                    sources.append(row["source"])
            expiries = [row["expires_at"] for row in rows]
            expires_at = None if None in expiries else max(expiries)
            self._conn.execute(
                "UPDATE memories SET tags = ?, tag_index = ?, source = ?, "
                "created_at = ?, expires_at = ? WHERE id = ?",
                (
                    _dump_tags(tags),
                    _tag_index(tags),
                    "; ".join(sources),
                    min(row["created_at"] for row in rows),
                    expires_at,
                    keep["id"],
                ),
            )
            for row in rows[1:]:
                self._conn.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
                removed += 1
        return removed

    def _ensure_fts(self, force_rebuild: bool) -> None:
        self._conn.execute(_FTS_TABLE)
        for trigger in _FTS_TRIGGERS:
            self._conn.execute(trigger)
        if force_rebuild or not self._fts_in_sync():
            self._conn.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")
            self.fts_rebuilt = True

    def _fts_in_sync(self) -> bool:
        """Cheap consistency check: same row count and max rowid as the table.

        A process without FTS5 (or a raw SQL edit) can write rows the index
        never saw; only then is the costly full rebuild needed.
        """
        table = self._conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM memories"
        ).fetchone()
        index = self._conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM memories_fts_docsize"
        ).fetchone()
        return tuple(table) == tuple(index)

    # -- internals ----------------------------------------------------------

    def _purge_expired(self) -> None:
        """Delete expired memories. Runs before every read/write operation.

        The check is an index probe; the write lock is only taken when there
        is actually something to delete.
        """
        now = _now_iso()
        due = self._conn.execute(
            "SELECT 1 FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ? LIMIT 1",
            (now,),
        ).fetchone()
        if due is None:
            return
        with self._write():
            self._conn.execute(
                "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            )

    def _row_to_dict(self, row: sqlite3.Row, include_age: bool = True) -> dict:
        memory = {
            "id": row["id"],
            "content": row["content"],
            "namespace": row["namespace"],
            "tags": _load_tags(row["tags"]),
            "source": row["source"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
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

    def _find_duplicate(self, namespace: str, content: str, exclude_id: int | None = None):
        row = self._conn.execute(
            "SELECT * FROM memories WHERE namespace = ? AND content = ?",
            (namespace, content),
        ).fetchone()
        if row is not None and row["id"] == exclude_id:
            return None
        return row

    @staticmethod
    def _filters(alias: str, namespace: str, tags: list[str]) -> tuple[str, list]:
        """SQL predicates for a namespace and required tags (all must match)."""
        sql = ""
        params: list = []
        if namespace:
            sql += f" AND {alias}namespace = ?"
            params.append(namespace)
        for tag in tags:
            sql += f" AND instr({alias}tag_index, ?) > 0"
            params.append(_tag_needle(tag))
        return sql, params

    @property
    def search_mode(self) -> str:
        return "fts5" if self.fts5_available else "like"

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    # -- tools --------------------------------------------------------------

    @_synchronized
    def remember(
        self,
        content: str,
        namespace: str = "default",
        tags: list[str] | None = None,
        ttl_days: int = 0,
        source: str = "",
    ) -> dict:
        """Store a fact. An exact duplicate is not stored again; its tags merge."""
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
        self._purge_expired()

        with self._write():
            existing = self._find_duplicate(namespace, content)
            if existing is not None:
                merged, added = _merge_tags(_load_tags(existing["tags"]), clean_tags)
                if added:
                    self._conn.execute(
                        "UPDATE memories SET tags = ?, tag_index = ?, updated_at = ? "
                        "WHERE id = ?",
                        (_dump_tags(merged), _tag_index(merged), _now_iso(), existing["id"]),
                    )
                    existing = self._get_row(existing["id"])
                result = self._row_to_dict(existing)
                result["deduplicated"] = True
                result["merged_tags"] = added
                message = (
                    f"Identical memory already exists in namespace "
                    f"'{namespace}' (id {existing['id']}) — not stored twice"
                )
                if added:
                    message += f"; added tags: {', '.join(added)}."
                else:
                    message += "."
                result["message"] = message
                return result

            cursor = self._conn.execute(
                """
                INSERT INTO memories
                    (content, namespace, tags, tag_index, source, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content,
                    namespace,
                    _dump_tags(clean_tags),
                    _tag_index(clean_tags),
                    source or "",
                    _now_iso(),
                    _expiry_from_now(ttl_days) if ttl_days > 0 else None,
                ),
            )
            row = self._get_row(cursor.lastrowid)
        result = self._row_to_dict(row)
        result["deduplicated"] = False
        result["merged_tags"] = []
        result["message"] = f"Stored memory {row['id']} in namespace '{namespace}'."
        return result

    @_synchronized
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
        limit = max(1, limit)
        namespace = (namespace or "").strip()
        required_tags = _normalize_tags(tags)

        if self.fts5_available:
            rows = self._recall_fts(query, namespace, required_tags, limit)
        else:
            rows = self._recall_like(query, namespace, required_tags, limit)

        hits = []
        for row, snippet in rows:
            memory = self._row_to_dict(row)
            memory["snippet"] = snippet
            hits.append(memory)
        return {
            "query": query,
            "search_mode": self.search_mode,
            "count": len(hits),
            "hits": hits,
        }

    def _recall_fts(self, query, namespace, tags, limit):
        where, params = self._filters("m.", namespace, tags)
        sql = f"""
            SELECT m.*,
                   snippet(memories_fts, 0, '[', ']', ' … ', {_SNIPPET_TOKENS}) AS snip,
                   bm25(memories_fts) AS score
            FROM memories_fts
            JOIN memories AS m ON m.id = memories_fts.rowid
            WHERE memories_fts MATCH ? {where}
            ORDER BY score ASC, m.created_at DESC, m.id DESC
            LIMIT ?
        """
        rows = self._conn.execute(
            sql, [_fts_match_expression(query), *params, limit]
        ).fetchall()
        return [(row, row["snip"]) for row in rows]

    def _recall_like(self, query, namespace, tags, limit):
        where, params = self._filters("", namespace, tags)
        terms = [t for t in query.split() if t]
        like = "".join(" AND content LIKE ? ESCAPE '\\'" for _ in terms)
        sql = (
            f"SELECT * FROM memories WHERE 1=1 {like} {where} "
            "ORDER BY created_at DESC, id DESC LIMIT ?"
        )
        rows = self._conn.execute(
            sql, ["%" + _escape_like(t) + "%" for t in terms] + params + [limit]
        ).fetchall()
        return [(row, _truncate(row["content"], _FALLBACK_SNIPPET_CHARS)) for row in rows]

    @_synchronized
    def get_memory(self, memory_id: int) -> dict:
        """Return one memory by id."""
        self._purge_expired()
        return self._row_to_dict(self._get_row(memory_id))

    @_synchronized
    def forget(self, memory_id: int) -> dict:
        """Delete a memory by id, confirming what was removed."""
        self._purge_expired()
        with self._write():
            row = self._get_row(memory_id)
            self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return {
            "deleted": True,
            "id": memory_id,
            "namespace": row["namespace"],
            "content": _truncate(row["content"]),
            "message": f"Forgot memory {memory_id}: {_truncate(row['content'])}",
        }

    @_synchronized
    def list_memories(
        self, namespace: str = "", tag: str = "", limit: int = 20
    ) -> dict:
        """List memories, most recent first, optionally filtered."""
        self._purge_expired()
        limit = max(1, limit)
        namespace = (namespace or "").strip()
        tags = _normalize_tags([tag] if tag else [])
        where, params = self._filters("", namespace, tags)
        rows = self._conn.execute(
            f"SELECT * FROM memories WHERE 1=1 {where} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        memories = [self._row_to_dict(row) for row in rows]
        return {"count": len(memories), "memories": memories}

    @_synchronized
    def update_memory(
        self,
        memory_id: int,
        content: str = "",
        add_tags: list[str] | None = None,
        ttl_days: int = -1,
        remove_tags: list[str] | None = None,
        namespace: str = "",
        source: str | None = None,
    ) -> dict:
        """Update a memory's content, tags, namespace, source and/or TTL.

        ttl_days: -1 leaves the TTL untouched, 0 removes it (memory becomes
        permanent), any positive value sets a new expiry from now.
        namespace: "" keeps the namespace, anything else moves the memory.
        source: ``None`` keeps it, a string replaces it.

        Content (or a namespace move) that would duplicate another memory in
        the target namespace is refused, naming the existing memory.
        """
        if ttl_days < -1:
            raise ValueError(
                "ttl_days must be -1 (leave unchanged), 0 (remove TTL), or a "
                "positive number of days."
            )
        content = (content or "").strip()
        new_namespace = (namespace or "").strip()
        added_tags = _normalize_tags(add_tags)
        dropped_tags = _normalize_tags(remove_tags)
        clash = {t.casefold() for t in added_tags} & {t.casefold() for t in dropped_tags}
        if clash:
            raise ValueError(
                f"Tag(s) {', '.join(sorted(clash))} are in both add_tags and "
                "remove_tags — pass each tag in only one of them."
            )
        if (
            not content
            and not new_namespace
            and not added_tags
            and not dropped_tags
            and source is None
            and ttl_days == -1
        ):
            raise ValueError(
                "Nothing to update — pass content, add_tags, remove_tags, "
                "namespace, source and/or ttl_days."
            )
        self._purge_expired()

        with self._write():
            row = self._get_row(memory_id)
            changes: dict[str, object] = {}

            target_content = content or row["content"]
            target_namespace = new_namespace or row["namespace"]
            if target_content != row["content"]:
                changes["content"] = target_content
            if target_namespace != row["namespace"]:
                changes["namespace"] = target_namespace
            if "content" in changes or "namespace" in changes:
                duplicate = self._find_duplicate(
                    target_namespace, target_content, exclude_id=memory_id
                )
                if duplicate is not None:
                    raise ValueError(
                        f"Memory {duplicate['id']} in namespace '{target_namespace}' "
                        f"already says exactly this — update memory "
                        f"{duplicate['id']} instead, or forget({memory_id}) if "
                        "this one is now redundant."
                    )

            tags = _load_tags(row["tags"])
            if added_tags:
                tags, _ = _merge_tags(tags, added_tags)
            if dropped_tags:
                drop = {t.casefold() for t in dropped_tags}
                tags = [t for t in tags if t.casefold() not in drop]
            if tags != _load_tags(row["tags"]):
                changes["tags"] = _dump_tags(tags)
                changes["tag_index"] = _tag_index(tags)

            if source is not None and source != row["source"]:
                changes["source"] = source

            if ttl_days == 0 and row["expires_at"] is not None:
                changes["expires_at"] = None
            elif ttl_days > 0:
                changes["expires_at"] = _expiry_from_now(ttl_days)

            updated_fields = [c for c in changes if c != "tag_index"]
            if changes:
                changes["updated_at"] = _now_iso()
                assignments = ", ".join(f"{column} = ?" for column in changes)
                self._conn.execute(
                    f"UPDATE memories SET {assignments} WHERE id = ?",
                    (*changes.values(), memory_id),
                )
            result = self._row_to_dict(self._get_row(memory_id))

        result["updated_fields"] = updated_fields
        result["message"] = (
            f"Updated memory {memory_id} ({', '.join(updated_fields)})."
            if updated_fields
            else f"Memory {memory_id} already matched the requested state."
        )
        return result

    @_synchronized
    def memory_stats(self) -> dict:
        """Vault statistics: totals, namespaces, tags, TTL usage, size, schema."""
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
        # Count on the case-folded tag_index (plain string splitting, no JSON
        # decoding per row), then look up the display casing of the winners.
        tag_counts: dict[str, int] = {}
        for index, rows in self._conn.execute(
            "SELECT tag_index, COUNT(*) FROM memories WHERE tag_index != '' GROUP BY tag_index"
        ):
            for key in index.strip(_TAG_SEP).split(_TAG_SEP):
                tag_counts[key] = tag_counts.get(key, 0) + rows
        top_tags: dict[str, int] = {}
        for key, count in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_TOP_TAGS]:
            sample = self._conn.execute(
                "SELECT tags FROM memories WHERE instr(tag_index, ?) > 0 ORDER BY id LIMIT 1",
                (_tag_needle(key),),
            ).fetchone()
            display = next(
                (t for t in _load_tags(sample[0] if sample else None) if t.casefold() == key),
                key,
            )
            top_tags[display] = count
        db_size = 0
        for suffix in ("", "-wal"):
            try:
                db_size += Path(str(self.db_path) + suffix).stat().st_size
            except OSError:
                pass
        return {
            "total_memories": total,
            "by_namespace": by_namespace,
            "top_tags": top_tags,
            "with_active_ttl": with_ttl,
            "db_size_bytes": db_size,
            "db_path": str(self.db_path),
            "search_mode": self.search_mode,
            "schema_version": self.schema_version,
        }

    @_synchronized
    def export_memories(self, namespace: str = "") -> dict:
        """Export memories as plain JSON-serializable dicts (for backup)."""
        self._purge_expired()
        namespace = (namespace or "").strip()
        where, params = self._filters("", namespace, [])
        rows = self._conn.execute(
            f"SELECT * FROM memories WHERE 1=1 {where} ORDER BY id", params
        ).fetchall()
        memories = [self._row_to_dict(row, include_age=False) for row in rows]
        return {
            "count": len(memories),
            "namespace": namespace or "(all)",
            "exported_at": _now_iso(),
            "schema_version": SCHEMA_VERSION,
            "memories": memories,
        }

    @_synchronized
    def import_memories(self, memories: list[dict]) -> dict:
        """Import memories previously produced by export_memories.

        Every item is validated before anything is written, and the import
        runs in a single transaction: it either fully succeeds or changes
        nothing. Timestamps are converted to canonical UTC. Exact duplicates
        (same content + namespace, in the vault or earlier in the batch) are
        skipped, and so are items whose expires_at is already in the past.
        """
        if not isinstance(memories, list):
            raise ValueError(
                "memories must be a list of dicts as produced by "
                "export_memories (the 'memories' array)."
            )
        now = _now_iso()
        prepared = [self._prepare_import_item(index, item, now) for index, item in enumerate(memories)]
        self._purge_expired()

        imported = skipped = expired = 0
        with self._write():
            for row in prepared:
                if row["expires_at"] is not None and row["expires_at"] <= now:
                    expired += 1
                    continue
                cursor = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO memories
                        (content, namespace, tags, tag_index, source,
                         created_at, updated_at, expires_at)
                    VALUES
                        (:content, :namespace, :tags, :tag_index, :source,
                         :created_at, :updated_at, :expires_at)
                    """,
                    row,
                )
                if cursor.rowcount == 1:
                    imported += 1
                else:
                    skipped += 1
        message = f"Imported {imported} memories, skipped {skipped} duplicates"
        message += f" and {expired} already-expired items." if expired else "."
        return {
            "imported": imported,
            "skipped": skipped,
            "expired": expired,
            "total_received": len(memories),
            "message": message,
        }

    @staticmethod
    def _prepare_import_item(index: int, item: object, now: str) -> dict:
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
        try:
            tags = _normalize_tags(raw_tags)
        except ValueError as exc:
            raise ValueError(f"Item {index}: {exc}") from exc

        def timestamp(field: str, default: str | None) -> str | None:
            value = item.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                return default
            try:
                return canonical_timestamp(str(value))
            except ValueError as exc:
                raise ValueError(
                    f"Item {index} has invalid '{field}' ({value!r}) — expected "
                    "ISO-8601, e.g. 2026-07-23T12:00:00Z or "
                    "2026-07-23T05:00:00-07:00."
                ) from exc

        return {
            "content": content,
            "namespace": namespace,
            "tags": _dump_tags(tags),
            "tag_index": _tag_index(tags),
            "source": str(item.get("source") or ""),
            "created_at": timestamp("created_at", now),
            "updated_at": timestamp("updated_at", None),
            "expires_at": timestamp("expires_at", None),
        }

    @_synchronized
    def close(self) -> None:
        """Close the underlying SQLite connection."""
        try:
            self._conn.execute("PRAGMA optimize")
        except sqlite3.Error:
            pass
        self._conn.close()

    def __enter__(self) -> "MemoryVault":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
