# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-07-26

### Added

- Eight MCP tools over a local SQLite vault: `remember`, `recall`, `forget`, `list_memories`, `update_memory`, `memory_stats`, `export_memories` and `import_memories`.
- Ranked full-text search with SQLite FTS5 (bm25 relevance, recency tiebreak), highlighted snippets and readable ages like "3 days ago"; multi-word queries use AND semantics.
- Automatic fallback to a term-wise LIKE search when the local SQLite build lacks FTS5, with LIKE wildcards escaped rather than interpreted; `memory_stats` reports the active `search_mode`.
- Namespaces, tags and optional TTLs — expired rows are purged on every read or write, so no background process is needed.
- Content deduplication per namespace, JSON export/import round-trips that preserve timestamps and TTLs, and a relocatable database via the `MEMORY_VAULT_DB` environment variable.
- Zero external dependencies in the core: everything runs on the Python standard library, covered by 18 tests that exercise `core.py` without the `mcp` package installed.

[0.1.0]: https://github.com/AleBrito124356/mcp-memory-vault/releases/tag/v0.1.0
