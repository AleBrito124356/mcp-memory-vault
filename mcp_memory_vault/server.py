"""mcp-memory-vault — MCP server that gives any agent persistent memory.

Entry point: FastMCP wiring only. All logic lives in core.py (pure stdlib).
Run with: python -m mcp_memory_vault.server  (stdio transport)
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .core import MemoryVault

mcp = FastMCP("mcp-memory-vault")
vault = MemoryVault()


@mcp.tool()
def remember(
    content: str,
    namespace: str = "default",
    tags: list[str] = [],
    ttl_days: int = 0,
    source: str = "",
) -> dict:
    """Store a fact in persistent memory so it survives across sessions.

    Use this whenever you learn something worth keeping: user preferences,
    project decisions, environment quirks, follow-ups. Exact duplicates
    (same content in the same namespace) are detected and returned with
    "deduplicated": true instead of being stored twice.

    Args:
        content: The fact to remember, as a self-contained sentence.
        namespace: Logical bucket (e.g. per project or per user). Defaults
            to "default".
        tags: Optional labels for later filtering (e.g. ["customer", "deploy"]).
        ttl_days: Days until the memory expires. 0 = never expires.
        source: Optional provenance note (e.g. "support ticket #123").

    Returns:
        The stored memory (id, content, namespace, tags, timestamps) plus
        "deduplicated" and a human-readable "message".
    """
    return vault.remember(
        content=content,
        namespace=namespace,
        tags=tags,
        ttl_days=ttl_days,
        source=source,
    )


@mcp.tool()
def recall(
    query: str,
    namespace: str = "",
    tags: list[str] = [],
    limit: int = 8,
) -> dict:
    """Search memories by full text and get the best matches first.

    All query terms must match (AND semantics). Results are ranked by
    relevance (SQLite FTS5 bm25) with recency as tiebreaker, and each hit
    includes a highlighted snippet, its tags, and a readable age like
    "3 days ago".

    Args:
        query: One or more search terms, e.g. "ACME deploy".
        namespace: Restrict the search to one namespace ("" = all).
        tags: Only return memories carrying ALL of these tags.
        limit: Maximum number of hits to return (default 8).

    Returns:
        {"query", "search_mode", "count", "hits": [...]} where each hit has
        id, content, snippet, namespace, tags, source, created_at, age.
    """
    return vault.recall(query=query, namespace=namespace, tags=tags, limit=limit)


@mcp.tool()
def forget(memory_id: int) -> dict:
    """Permanently delete one memory by id.

    Args:
        memory_id: The id of the memory to delete (from recall or
            list_memories).

    Returns:
        Confirmation with the id and a truncated preview of what was deleted.
    """
    return vault.forget(memory_id=memory_id)


@mcp.tool()
def list_memories(namespace: str = "", tag: str = "", limit: int = 20) -> dict:
    """Browse stored memories, most recent first, without a search query.

    Args:
        namespace: Only list memories in this namespace ("" = all).
        tag: Only list memories carrying this tag ("" = any).
        limit: Maximum number of memories to return (default 20).

    Returns:
        {"count", "memories": [...]} with expires_at included when a TTL
        is set.
    """
    return vault.list_memories(namespace=namespace, tag=tag, limit=limit)


@mcp.tool()
def update_memory(
    memory_id: int,
    content: str = "",
    add_tags: list[str] = [],
    ttl_days: int = -1,
) -> dict:
    """Edit an existing memory: rewrite content, add tags, or change TTL.

    Args:
        memory_id: The id of the memory to update.
        content: New content ("" = keep current content).
        add_tags: Tags to add on top of the existing ones.
        ttl_days: -1 = leave TTL unchanged, 0 = remove TTL (make permanent),
            any positive value = expire that many days from now.

    Returns:
        The updated memory plus "updated_fields" listing what changed.
    """
    return vault.update_memory(
        memory_id=memory_id,
        content=content,
        add_tags=add_tags,
        ttl_days=ttl_days,
    )


@mcp.tool()
def memory_stats() -> dict:
    """Get vault statistics and health information.

    Returns:
        Total memories, counts per namespace, how many have an active TTL,
        database file size in bytes, database path, and the active
        search_mode ("fts5" or "like" fallback).
    """
    return vault.memory_stats()


@mcp.tool()
def export_memories(namespace: str = "") -> dict:
    """Export memories as JSON-serializable dicts for backup or migration.

    Args:
        namespace: Only export this namespace ("" = export everything).

    Returns:
        {"count", "namespace", "memories": [...]} — feed the "memories"
        array to import_memories on another vault to migrate.
    """
    return vault.export_memories(namespace=namespace)


@mcp.tool()
def import_memories(memories: list[dict]) -> dict:
    """Import memories from a previous export_memories call.

    Each item needs at least a non-empty "content"; namespace, tags,
    source, created_at, and expires_at are preserved when present. Exact
    duplicates (same content + namespace) are skipped.

    Args:
        memories: The "memories" array produced by export_memories.

    Returns:
        {"imported", "skipped", "total_received", "message"}.
    """
    return vault.import_memories(memories=memories)


def main() -> None:
    """Entry point for the console script."""
    mcp.run()


if __name__ == "__main__":
    main()
