"""Command-line interface: inspect, curate, back up and diagnose a vault.

``mcp-memory-vault`` with no command (or ``serve``) runs the MCP server on
stdio, so existing Claude Desktop / Claude Code configurations keep working.
Every other command talks to :mod:`core` directly and works without the
``mcp`` package installed.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sqlite3
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .core import SCHEMA_VERSION, MATCH_MODES, MemoryVault, fts5_supported

MCP_MIN = (1, 10)
MCP_MAX_EXCLUSIVE = (3, 0)


class CliError(Exception):
    """A user-facing error: printed without a traceback, exit status 1."""


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _stdout_is_utf8() -> bool:
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "")
    return encoding in ("utf8", "utf8sig")


def _dump_json(data: Any) -> str:
    # On a non-UTF-8 console (e.g. cp1252 on Windows) escape non-ASCII instead
    # of mangling it: the output stays valid, lossless JSON either way.
    return json.dumps(data, indent=2, ensure_ascii=not _stdout_is_utf8())


def _width() -> int:
    return max(60, min(shutil.get_terminal_size((100, 20)).columns, 160))


def _clip(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _memory_line(memory: dict, text: str | None = None) -> list[str]:
    width = _width()
    head = f"#{memory['id']:<5} [{memory['namespace']}] "
    lines = [head + _clip(text or memory["content"], width - len(head))]
    details = []
    if memory.get("tags"):
        details.append("tags: " + ", ".join(memory["tags"]))
    if memory.get("age"):
        details.append(memory["age"])
    if memory.get("expires_at"):
        details.append("expires " + memory["expires_at"])
    if memory.get("matched_terms") is not None:
        details.append("matched: " + (", ".join(memory["matched_terms"]) or "-"))
    if details:
        lines.append(" " * 7 + " · ".join(details))
    return lines


def _print_memory_block(memory: dict) -> None:
    fields = [
        ("id", memory["id"]),
        ("namespace", memory["namespace"]),
        ("content", memory["content"]),
        ("tags", ", ".join(memory["tags"]) or "-"),
        ("source", memory["source"] or "-"),
        ("created", f"{memory['created_at']} ({memory.get('age', '')})"),
        ("updated", memory.get("updated_at") or "-"),
        ("expires", memory.get("expires_at") or "never"),
    ]
    for label, value in fields:
        print(f"{label:>9}: {value}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _open(args) -> MemoryVault:
    return MemoryVault(db_path=args.db) if args.db else MemoryVault()


def cmd_add(args) -> dict:
    content = sys.stdin.read() if args.content == "-" else args.content
    with _open(args) as vault:
        result = vault.remember(
            content,
            namespace=args.namespace,
            tags=args.tag,
            ttl_days=args.ttl_days,
            source=args.source,
        )
    if not args.json:
        print(result["message"])
        print("\n".join(_memory_line(result)))
    return result


def cmd_search(args) -> dict:
    with _open(args) as vault:
        result = vault.recall(
            args.query,
            namespace=args.namespace,
            tags=args.tag,
            limit=args.limit,
            match=args.match,
        )
    if not args.json:
        noun = "hit" if result["count"] == 1 else "hits"
        print(
            f'{result["count"]} {noun} for "{args.query}" '
            f'(terms: {", ".join(result["terms"]) or "-"}; '
            f'{result["search_mode"]}, match: {result["match_mode"] or "-"})'
        )
        for note in (result.get("note"), result.get("hint")):
            if note:
                print(f"note: {note}")
        for hit in result["hits"]:
            print("\n".join(_memory_line(hit, hit["snippet"])))
    return result


def cmd_list(args) -> dict:
    with _open(args) as vault:
        result = vault.list_memories(namespace=args.namespace, tag=args.tag, limit=args.limit)
    if not args.json:
        if not result["memories"]:
            print("No memories match.")
        for memory in result["memories"]:
            print("\n".join(_memory_line(memory)))
    return result


def cmd_show(args) -> dict:
    with _open(args) as vault:
        memory = vault.get_memory(args.id)
    if not args.json:
        _print_memory_block(memory)
    return memory


def cmd_edit(args) -> dict:
    with _open(args) as vault:
        result = vault.update_memory(
            args.id,
            content=args.content or "",
            add_tags=args.add_tag,
            remove_tags=args.remove_tag,
            namespace=args.namespace or "",
            source=args.source,
            ttl_days=args.ttl_days,
        )
    if not args.json:
        print(result["message"])
        _print_memory_block(result)
    return result


def cmd_forget(args) -> dict:
    results, errors = [], []
    with _open(args) as vault:
        for memory_id in args.ids:
            try:
                results.append(vault.forget(memory_id))
            except ValueError as exc:
                errors.append({"id": memory_id, "error": str(exc)})
    if not args.json:
        for result in results:
            print(result["message"])
        for error in errors:
            print(f"error: {error['error']}", file=sys.stderr)
    outcome = {"forgotten": results, "errors": errors}
    if errors:
        outcome["_exit"] = 1
    return outcome


def cmd_stats(args) -> dict:
    with _open(args) as vault:
        stats = vault.memory_stats()
    if not args.json:
        print(f"memories      {stats['total_memories']}  ({stats['with_active_ttl']} with a TTL)")
        namespaces = ", ".join(f"{ns} ({n})" for ns, n in stats["by_namespace"].items())
        print(f"namespaces    {namespaces or '-'}")
        tags = ", ".join(f"{tag} ({n})" for tag, n in stats["top_tags"].items())
        print(f"top tags      {tags or '-'}")
        print(f"search        {stats['search_mode']}")
        print(f"schema        v{stats['schema_version']}")
        print(f"database      {stats['db_path']} ({stats['db_size_bytes'] / 1024:.1f} KiB)")
    return stats


def cmd_export(args) -> dict:
    with _open(args) as vault:
        export = vault.export_memories(namespace=args.namespace)
    if args.output:
        path = Path(args.output)
        path.write_text(json.dumps(export, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        summary = {"exported": export["count"], "path": str(path.resolve())}
        if args.json:
            return summary
        print(f"Exported {export['count']} memories to {path}", file=sys.stderr)
        return {"_silent": True}
    print(_dump_json(export))
    return {"_silent": True}


def cmd_import(args) -> dict:
    if args.file == "-":
        raw = sys.stdin.buffer.read().decode("utf-8-sig")
    else:
        try:
            raw = Path(args.file).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise CliError(f"cannot read {args.file}: {exc.strerror or exc}") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"{args.file} is not valid JSON ({exc.msg} at line {exc.lineno}).") from None
    memories = data.get("memories") if isinstance(data, dict) else data
    if not isinstance(memories, list):
        raise CliError(
            f"{args.file} must contain an export (an object with a 'memories' "
            "array) or a bare array of memories."
        )
    with _open(args) as vault:
        result = vault.import_memories(memories)
    if not args.json:
        print(result["message"])
    return result


# -- doctor -------------------------------------------------------------------

def _version_tuple(text: str) -> tuple[int, ...]:
    """Leading numeric release parts: "2.0.0rc1" -> (2, 0, 0)."""
    parts = []
    for piece in text.split("."):
        digits = re.match(r"\d+", piece)
        if not digits:
            break
        parts.append(int(digits.group(0)))
        if digits.group(0) != piece:
            break
    return tuple(parts)


def _db_location(args) -> tuple[Path, str]:
    if args.db:
        return Path(args.db).expanduser(), "--db"
    if os.environ.get("MEMORY_VAULT_DB"):
        return Path(os.environ["MEMORY_VAULT_DB"]).expanduser(), "MEMORY_VAULT_DB"
    return Path.home() / ".mcp-memory-vault" / "memories.db", "default location"


def _inspect_database(path: Path, checks: list) -> None:
    """Read-only inspection: doctor never migrates or rebuilds anything."""
    def add(status: str, name: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    if not path.exists():
        parent = path.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        if os.access(parent, os.W_OK):
            add("ok", "database", f"{path} does not exist yet; it will be created on first use")
        else:
            add("fail", "database", f"{path} does not exist and {parent} is not writable")
        return

    if not (os.access(path, os.W_OK) and os.access(path.parent, os.W_OK)):
        add("fail", "database", f"{path} (or its folder, needed for the WAL files) is not writable")
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        add("fail", "database", f"cannot open {path}: {exc}")
        return
    try:
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            add("fail", "integrity", f"PRAGMA quick_check: {integrity}")
            return
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memories'"
        ).fetchone()
        if not has_table:
            add("fail", "database", f"{path} is not a mcp-memory-vault database (no memories table)")
            return
        total = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        add("ok", "database", f"{path} ({total} memories, integrity ok)")
        if version > SCHEMA_VERSION:
            add("fail", "schema", f"v{version} is newer than this package understands (v{SCHEMA_VERSION}); upgrade mcp-memory-vault")
        elif version < SCHEMA_VERSION:
            add("warn", "schema", f"v{version}; it will be upgraded in place to v{SCHEMA_VERSION} the next time the vault is opened")
        else:
            add("ok", "schema", f"v{version} (current)")
        try:
            table = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM memories").fetchone()
            index = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM memories_fts_docsize").fetchone()
        except sqlite3.OperationalError:
            add("warn", "search index", "no full-text index yet; it will be built on next open")
        else:
            if tuple(table) == tuple(index):
                add("ok", "search index", "in sync with the memories table")
            else:
                add("warn", "search index", "out of sync; it will be rebuilt on next open")
    finally:
        conn.close()


def cmd_doctor(args) -> dict:
    checks: list[dict] = []

    def add(status: str, name: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    add("ok", "mcp-memory-vault", __version__)
    py = sys.version_info
    add("ok" if py >= (3, 10) else "fail", "python", f"{py.major}.{py.minor}.{py.micro}")
    probe = sqlite3.connect(":memory:")
    try:
        add("ok", "sqlite", sqlite3.sqlite_version)
        if fts5_supported(probe):
            add("ok", "fts5", "available (ranked full-text search)")
        else:
            add("warn", "fts5", "missing in this SQLite build; recall uses the slower LIKE fallback")
    finally:
        probe.close()

    path, origin = _db_location(args)
    add("ok", "database path", f"{path} (from {origin})")
    _inspect_database(path, checks)

    try:
        mcp_version = metadata.version("mcp")
    except metadata.PackageNotFoundError:
        add("fail", "mcp", "not installed: the MCP server cannot start (pip install 'mcp>=1.10,<3'); the CLI still works")
    else:
        parsed = _version_tuple(mcp_version)
        if not (MCP_MIN <= parsed[:2] < MCP_MAX_EXCLUSIVE):
            add("fail", "mcp", f"{mcp_version} is outside the supported range >=1.10,<3")
        else:
            try:
                from . import server  # noqa: PLC0415 - import only to prove it works
            except Exception as exc:  # pragma: no cover - reported, not raised
                add("fail", "mcp", f"{mcp_version} installed, but the server does not import: {exc}")
            else:
                add("ok", "mcp", f"{mcp_version} (SDK {server.MCP_MAJOR}.x, server imports fine)")

    ok = not any(check["status"] == "fail" for check in checks)
    if not args.json:
        for check in checks:
            print(f"[{check['status']:>4}] {check['name']:<17} {check['detail']}")
        print("\nAll good." if ok else "\nProblems found (see [fail] lines above).")
    return {"ok": ok, "checks": checks, "_exit": 0 if ok else 1}


def cmd_serve(args) -> dict:
    if args.db:
        os.environ["MEMORY_VAULT_DB"] = str(Path(args.db).expanduser())
    try:
        from . import server  # noqa: PLC0415
    except ImportError as exc:
        print(
            f"error: the MCP server needs the 'mcp' package ({exc}).\n"
            "Install it with: pip install 'mcp>=1.10,<3'",
            file=sys.stderr,
        )
        return {"_exit": 2, "_silent": True}
    server.main()
    return {"_silent": True}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    # --db / --json are accepted before or after the command.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, metavar="PATH",
                        help="vault file (default: $MEMORY_VAULT_DB or ~/.mcp-memory-vault/memories.db)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print machine-readable JSON")

    parser = argparse.ArgumentParser(
        prog="mcp-memory-vault",
        description="Persistent memory for AI agents. With no command, runs the MCP server on stdio.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def command(name: str, handler: Callable, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text, parents=[common])
        p.set_defaults(handler=handler)
        return p

    command("serve", cmd_serve, "run the MCP server on stdio (the default)")

    p = command("add", cmd_add, "remember a fact")
    p.add_argument("content", help='the fact to store ("-" reads it from stdin)')
    p.add_argument("-n", "--namespace", default="default")
    p.add_argument("-t", "--tag", action="append", default=[], help="tag (repeatable)")
    p.add_argument("--ttl-days", type=int, default=0, help="expire after N days (0 = never)")
    p.add_argument("--source", default="", help="provenance note")

    p = command("search", cmd_search, "search memories (plain questions work)")
    p.add_argument("query")
    p.add_argument("-n", "--namespace", default="")
    p.add_argument("-t", "--tag", action="append", default=[], help="required tag (repeatable)")
    p.add_argument("--match", choices=MATCH_MODES, default="auto")
    p.add_argument("--limit", type=int, default=8)

    p = command("list", cmd_list, "list memories, most recent first")
    p.add_argument("-n", "--namespace", default="")
    p.add_argument("-t", "--tag", default="")
    p.add_argument("--limit", type=int, default=20)

    p = command("show", cmd_show, "show one memory")
    p.add_argument("id", type=int)

    p = command("edit", cmd_edit, "change a memory's content, tags, namespace, source or TTL")
    p.add_argument("id", type=int)
    p.add_argument("--content")
    p.add_argument("--add-tag", action="append", default=[])
    p.add_argument("--remove-tag", action="append", default=[])
    p.add_argument("--namespace", help="move to this namespace")
    p.add_argument("--source")
    p.add_argument("--ttl-days", type=int, default=-1, help="-1 keep, 0 remove, N expire in N days")

    p = command("forget", cmd_forget, "delete memories by id")
    p.add_argument("ids", type=int, nargs="+", metavar="ID")

    command("stats", cmd_stats, "vault statistics")

    p = command("export", cmd_export, "export memories as JSON (backup / migration)")
    p.add_argument("-o", "--output", help="write to this file instead of stdout")
    p.add_argument("-n", "--namespace", default="")

    p = command("import", cmd_import, "import an export file (duplicates are skipped)")
    p.add_argument("file", help='JSON file from "export" ("-" reads stdin)')

    command("doctor", cmd_doctor, "check the installation and the vault; exits 1 on problems")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.db = getattr(args, "db", None)
    args.json = getattr(args, "json", False)
    handler = getattr(args, "handler", cmd_serve)

    if handler is not cmd_serve and isinstance(sys.stdout, io.TextIOWrapper):
        # Human output must never crash on a console that cannot encode "…".
        sys.stdout.reconfigure(errors="replace")

    try:
        result = handler(args)
    except (ValueError, CliError, RuntimeError) as exc:
        message = str(exc)
    except sqlite3.Error as exc:
        message = f"database error: {exc}"
    else:
        exit_code = result.pop("_exit", 0)
        if args.json and not result.pop("_silent", False):
            print(_dump_json(result))
        return exit_code

    if args.json:
        print(_dump_json({"error": message}))
    print(f"error: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
