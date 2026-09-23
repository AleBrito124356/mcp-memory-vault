# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — 0.2.0

### Fixed

- **The server did not start on a fresh install.** `mcp>=1.2.0` had no upper bound, so pip and uvx resolved mcp 2.x, which removed `mcp.server.fastmcp`. The server now runs on mcp 1.10+ and 2.x (tested on 1.10.0, 1.30.0 and 2.2.0), and the dependency is `mcp>=1.10,<3`.
- **Concurrent tool calls corrupted the vault.** mcp 2.x runs tools on worker threads, and one SQLite connection was shared without a lock: 8 threads × 150 operations crashed and kept 164 of 1,200 writes. All operations now run under a lock, and every write is one transaction.
- Error messages from the vault ("Memory 999 not found… use list_memories") now reach the model on both SDK generations, instead of an opaque "Error executing tool".
- Timestamps with UTC offsets are converted to UTC instead of being stored raw. An imported memory set to expire in 2 hours was purged on the very next call.
- `update_memory` can no longer turn a memory into an exact duplicate of another one. A UNIQUE index enforces `(namespace, content)` for every write path.
- Remembering an existing fact with new tags merges them (`merged_tags`) instead of silently dropping them.
- Importing the server module no longer creates `~/.mcp-memory-vault` as a side effect.
- README: the Quickstart promised `uvx mcp-memory-vault` and `pip install mcp-memory-vault`, but the package has never been published to PyPI. It now installs from GitHub.

### Added

- **Natural-language recall.** Queries are normalised: punctuation is split off, English and Spanish filler words are dropped, and words match by prefix. `recall` gains `match="auto" | "all" | "any"`. The default, `auto`, returns memories matching every term, or partial matches ranked by terms matched when there are none. Results include the normalised `terms`, `match_mode` and per-hit `matched_terms`, plus a `note` or `hint` when the hits are partial or empty. On the regression table in `tests/test_recall_quality.py`, 17 of 17 queries get the right memory first, compared with 5 of 17 before.
- `update_memory` gains `remove_tags`, `namespace` (move) and `source`. Memories carry `updated_at`.
- Tags are case-insensitive and keep the casing they were first written with.
- `memory_stats` reports `top_tags` and `schema_version`. `export_memories` includes `exported_at` and `schema_version`. `import_memories` reports `expired` items.
- Server instructions that tell the model when to remember and how to recall. Every tool has a title, per-argument descriptions and MCP annotations (read-only and destructive hints).
- **CLI** (`mcp-memory-vault <command>`): `add`, `search`, `list`, `show`, `edit`, `forget`, `stats`, `export`, `import` and `doctor`, with global `--db` and `--json`. With no command, it still runs the MCP server. Everything except `serve` works without the `mcp` package installed.
- `python -m mcp_memory_vault` runs the same CLI.
- Tests: 18 → 112, covering the server through real MCP sessions, raw JSON-RPC over stdio, threads, migrations, integrity regressions, recall quality and the CLI.

### Changed

- **Schema v2, migrated automatically.** The first time 0.2.0 opens a 0.1.0 vault, it upgrades it in place in one transaction: timestamps are canonicalised, exact duplicates are merged (tags unioned, earliest `created_at` kept), and indexes are added. The full-text index is now rebuilt only after a migration or when it is out of sync, not on every start.
- Indexes on the dedup lookup, the expiry purge and listing. Import runs in one transaction and is all-or-nothing. At 50k memories, an import takes a few seconds instead of about 3 minutes, and a tag-filtered `list_memories` takes 0.3 ms instead of 133 ms.
- Reads no longer take the write lock unless something has expired.
- `recall` defaults to `match="auto"`. Pass `match="all"` for the previous strict AND. A query made only of punctuation now returns a `hint` instead of being searched literally.
- The `mcp-memory-vault` console script now points at the CLI. Running it with no arguments behaves exactly as before.

## [0.1.0] — 2026-07-26

### Added

- Eight MCP tools over a local SQLite vault: `remember`, `recall`, `forget`, `list_memories`, `update_memory`, `memory_stats`, `export_memories` and `import_memories`.
- Ranked full-text search with SQLite FTS5 (bm25 relevance, recency tiebreak), highlighted snippets and readable ages like "3 days ago"; multi-word queries use AND semantics.
- Automatic fallback to a term-wise LIKE search when the local SQLite build lacks FTS5, with LIKE wildcards escaped rather than interpreted; `memory_stats` reports the active `search_mode`.
- Namespaces, tags and optional TTLs — expired rows are purged on every read or write, so no background process is needed.
- Content deduplication per namespace, JSON export/import round-trips that preserve timestamps and TTLs, and a relocatable database via the `MEMORY_VAULT_DB` environment variable.
- Zero external dependencies in the core: everything runs on the Python standard library, covered by 18 tests that exercise `core.py` without the `mcp` package installed.

[0.1.0]: https://github.com/AleBrito124356/mcp-memory-vault/releases/tag/v0.1.0
