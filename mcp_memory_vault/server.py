"""mcp-memory-vault — MCP server that gives any agent persistent memory.

This module is MCP wiring only; all logic lives in :mod:`core` (pure stdlib).
It runs on both major versions of the official Python SDK:

* ``mcp`` 2.x, where the high-level server is ``mcp.server.mcpserver.MCPServer``
  and synchronous tools run on worker threads;
* ``mcp`` 1.x (>= 1.10), where it is ``mcp.server.fastmcp.FastMCP``.

Importing the module has no side effects: the SQLite vault is opened lazily on
the first tool call, at ``MEMORY_VAULT_DB`` or ``~/.mcp-memory-vault/memories.db``.

Run with: ``mcp-memory-vault`` or ``python -m mcp_memory_vault.server`` (stdio).
"""

# No ``from __future__ import annotations`` here: older 1.x SDKs inspect the
# tool signatures at runtime and choke on string annotations.
import sqlite3
import threading
from typing import Annotated, Any

from pydantic import Field

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _ServerClass
    from mcp.server.mcpserver.exceptions import ToolError

    MCP_MAJOR = 2
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _ServerClass
    from mcp.server.fastmcp.exceptions import ToolError

    MCP_MAJOR = 1

from mcp.types import ToolAnnotations

from . import __version__
from .core import MemoryVault

INSTRUCTIONS = """\
Memory Vault is persistent memory that survives across sessions and conversations.

REMEMBER as soon as you learn something a future session would need: user \
preferences, decisions and their reasons, project conventions, environment \
quirks, names/ids/contacts, commitments and follow-ups. Write one \
self-contained sentence per memory that names its subject ("Customer ACME \
prefers production deploys on Fridays", not "they prefer Fridays"). Use one \
namespace per project or user, 1-3 short tags, and ttl_days for context that \
goes stale. Never store secrets (passwords, API keys, tokens).

RECALL at the start of a task and before asking the user something they may \
already have told you. Query with the key nouns (people, projects, customers, \
tools); plain questions work too. If nothing comes back, try fewer or \
different words, or browse with list_memories.

KEEP IT ACCURATE: when a fact changes, update_memory the existing memory \
instead of storing a contradicting one, and forget memories that are wrong."""

_server_kwargs: dict[str, Any] = {"instructions": INSTRUCTIONS}
if MCP_MAJOR >= 2:
    _server_kwargs["version"] = __version__

mcp = _ServerClass("mcp-memory-vault", **_server_kwargs)
if MCP_MAJOR == 1 and hasattr(mcp, "_mcp_server"):
    # FastMCP has no version argument and would report the SDK's version as
    # serverInfo.version; report ours instead.
    mcp._mcp_server.version = __version__

_vault: MemoryVault | None = None
_vault_lock = threading.Lock()


def get_vault() -> MemoryVault:
    """Return the process-wide vault, opening it on first use."""
    global _vault
    if _vault is None:
        with _vault_lock:
            if _vault is None:
                _vault = MemoryVault()
    return _vault


def set_vault(vault: MemoryVault | None) -> MemoryVault | None:
    """Swap the vault used by the tools (tests, embedding). Returns the old one."""
    global _vault
    with _vault_lock:
        previous, _vault = _vault, vault
    return previous


def _call(method: str, **kwargs: Any) -> dict:
    """Invoke a vault method, turning anticipated failures into ToolError.

    The SDK only forwards the text of a ``ToolError`` to the model; any other
    exception is reported as an opaque crash (``Error executing tool ...``).
    core raises ``ValueError`` with actionable hints ("use list_memories to
    find valid ids"), so those must reach the model verbatim.
    """
    try:
        return getattr(get_vault(), method)(**kwargs)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    except sqlite3.OperationalError as exc:
        raise ToolError(
            f"The memory database is unavailable ({exc}). Try again in a moment; "
            "if it keeps failing, run `mcp-memory-vault doctor`."
        ) from None


_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    title="Remember a fact",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
def remember(
    content: Annotated[
        str, Field(description="The fact to remember, as one self-contained sentence that names its subject.")
    ],
    namespace: Annotated[
        str, Field(description="Logical bucket, e.g. one per project or user.")
    ] = "default",
    tags: Annotated[
        list[str], Field(description='Optional short labels for filtering, e.g. ["customer", "deploy"].')
    ] = [],
    ttl_days: Annotated[
        int, Field(description="Days until the memory expires; 0 = never expires.")
    ] = 0,
    source: Annotated[
        str, Field(description='Optional provenance, e.g. "user message" or "ticket #123".')
    ] = "",
) -> dict:
    """Store a fact in persistent memory so it survives across sessions.

    Use this whenever you learn something worth keeping: user preferences,
    project decisions, environment quirks, follow-ups. Exact duplicates
    (same content in the same namespace) are never stored twice: the
    existing memory is returned with "deduplicated": true, and any new tags
    are merged into it (listed in "merged_tags"). Tags are case-insensitive.

    Returns the stored memory (id, content, namespace, tags, timestamps) plus
    "deduplicated", "merged_tags" and a human-readable "message".
    """
    return _call(
        "remember",
        content=content,
        namespace=namespace,
        tags=tags,
        ttl_days=ttl_days,
        source=source,
    )


@mcp.tool(title="Search memories", annotations=_READ_ONLY)
def recall(
    query: Annotated[
        str, Field(description='Search terms, e.g. "ACME deploy".')
    ],
    namespace: Annotated[
        str, Field(description='Only search this namespace ("" = all namespaces).')
    ] = "",
    tags: Annotated[
        list[str], Field(description="Only return memories carrying ALL of these tags.")
    ] = [],
    limit: Annotated[int, Field(description="Maximum number of hits.")] = 8,
) -> dict:
    """Search memories by full text and get the best matches first.

    All query terms must match (AND semantics). Results are ranked by
    relevance (SQLite FTS5 bm25) with recency as tiebreaker, and each hit
    includes a highlighted snippet, its tags, and a readable age like
    "3 days ago".

    Returns {"query", "search_mode", "count", "hits": [...]} where each hit
    has id, content, snippet, namespace, tags, source, created_at, age.
    """
    return _call("recall", query=query, namespace=namespace, tags=tags, limit=limit)


@mcp.tool(
    title="Forget a memory",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    ),
)
def forget(
    memory_id: Annotated[
        int, Field(description="Id of the memory to delete (from recall or list_memories).")
    ],
) -> dict:
    """Permanently delete one memory by id.

    Returns confirmation with the id and a truncated preview of what was
    deleted.
    """
    return _call("forget", memory_id=memory_id)


@mcp.tool(title="List memories", annotations=_READ_ONLY)
def list_memories(
    namespace: Annotated[
        str, Field(description='Only list this namespace ("" = all).')
    ] = "",
    tag: Annotated[str, Field(description='Only list memories with this tag ("" = any).')] = "",
    limit: Annotated[int, Field(description="Maximum number of memories.")] = 20,
) -> dict:
    """Browse stored memories, most recent first, without a search query.

    Returns {"count", "memories": [...]} with expires_at included when a TTL
    is set.
    """
    return _call("list_memories", namespace=namespace, tag=tag, limit=limit)


@mcp.tool(
    title="Update a memory",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
    ),
)
def update_memory(
    memory_id: Annotated[int, Field(description="Id of the memory to update.")],
    content: Annotated[
        str, Field(description='New content ("" = keep the current content).')
    ] = "",
    add_tags: Annotated[
        list[str], Field(description="Tags to add on top of the existing ones.")
    ] = [],
    ttl_days: Annotated[
        int,
        Field(description="-1 = leave the TTL unchanged, 0 = remove it (permanent), N > 0 = expire N days from now."),
    ] = -1,
    remove_tags: Annotated[
        list[str], Field(description="Tags to remove (case-insensitive).")
    ] = [],
    namespace: Annotated[
        str, Field(description='Move the memory to this namespace ("" = keep).')
    ] = "",
    source: Annotated[
        str, Field(description='New provenance note ("" = keep the current one).')
    ] = "",
) -> dict:
    """Edit an existing memory: content, tags, namespace, source or TTL.

    Prefer this over storing a second, contradicting memory when a fact
    changes. A change that would make this memory an exact duplicate of
    another one in the target namespace is refused, and the error names the
    existing memory.

    Returns the updated memory plus "updated_fields" listing what changed.
    """
    return _call(
        "update_memory",
        memory_id=memory_id,
        content=content,
        add_tags=add_tags,
        ttl_days=ttl_days,
        remove_tags=remove_tags,
        namespace=namespace,
        source=source or None,
    )


@mcp.tool(title="Vault statistics", annotations=_READ_ONLY)
def memory_stats() -> dict:
    """Get vault statistics and health information.

    Returns total memories, counts per namespace, the most used tags (handy
    to pick tags consistently), how many have an active TTL, database file
    size and path, schema_version, and the active search_mode ("fts5" or
    "like" fallback).
    """
    return _call("memory_stats")


@mcp.tool(title="Export memories", annotations=_READ_ONLY)
def export_memories(
    namespace: Annotated[
        str, Field(description='Only export this namespace ("" = everything).')
    ] = "",
) -> dict:
    """Export memories as JSON-serializable dicts for backup or migration.

    Returns {"count", "namespace", "memories": [...]}; feed the "memories"
    array to import_memories on another vault to migrate.
    """
    return _call("export_memories", namespace=namespace)


@mcp.tool(
    title="Import memories",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
def import_memories(
    memories: Annotated[
        list[dict], Field(description='The "memories" array produced by export_memories.')
    ],
) -> dict:
    """Import memories from a previous export_memories call.

    Each item needs at least a non-empty "content"; namespace, tags, source,
    created_at, updated_at and expires_at are preserved when present
    (timestamps with offsets are converted to UTC). Everything is validated
    first and written in one transaction, so a bad item changes nothing.
    Exact duplicates (same content + namespace) and items that have already
    expired are skipped.

    Returns {"imported", "skipped", "expired", "total_received", "message"}.
    """
    return _call("import_memories", memories=memories)


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
