"""The mcp-memory-vault CLI, driven through real subprocesses."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mcp_memory_vault.core import SCHEMA_VERSION  # noqa: E402

# Run the CLI as if the mcp package were not installed at all.
NO_MCP = """
import importlib.metadata as md, sys
_version = md.version
def version(name):
    if name == "mcp":
        raise md.PackageNotFoundError(name)
    return _version(name)
md.version = version
sys.modules["mcp"] = None  # any "import mcp..." now raises ImportError
from mcp_memory_vault.cli import main
sys.exit(main(sys.argv[1:]))
"""


def run(*args: str, stdin: str | None = None, without_mcp: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONIOENCODING="utf-8")
    env.pop("MEMORY_VAULT_DB", None)
    command = [sys.executable, "-c", NO_MCP] if without_mcp else [sys.executable, "-m", "mcp_memory_vault"]
    return subprocess.run(
        [*command, *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=REPO,
        timeout=60,
    )


def ok(*args: str, **kwargs) -> subprocess.CompletedProcess:
    result = run(*args, **kwargs)
    assert result.returncode == 0, f"{args} failed:\n{result.stdout}\n{result.stderr}"
    return result


def as_json(*args: str, **kwargs):
    return json.loads(ok(*args, "--json", **kwargs).stdout)


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "cli.db")
    ok("--db", path, "add", "Customer ACME prefers deploys on Fridays", "-n", "support",
       "-t", "customer", "-t", "deploy", "--source", "user note")
    ok("--db", path, "add", "ACME's billing contact is Jane Doe", "-n", "support", "-t", "billing")
    ok("--db", path, "add", "Globex staging database resets nightly", "-n", "ops", "--ttl-days", "30")
    return path


def test_add_search_list_show_human_output(db):
    added = ok("add", "Customer ACME prefers deploys on Fridays", "-n", "support", "-t", "priority", "--db", db)
    assert "Identical memory already exists" in added.stdout
    assert "added tags: priority" in added.stdout

    search = ok("--db", db, "search", "when does ACME prefer to deploy?")
    assert '1 hit for "when does ACME prefer to deploy?"' in search.stdout
    assert "terms: acme, prefer, deploy" in search.stdout
    assert "Customer [ACME] [prefers] [deploys] on Fridays" in search.stdout
    assert "matched: acme, prefer, deploy" in search.stdout

    listing = ok("--db", db, "list", "-n", "support")
    lines = [line for line in listing.stdout.splitlines() if line.startswith("#")]
    assert len(lines) == 2 and "billing contact" in lines[0]  # most recent first

    show = ok("--db", db, "show", "1")
    assert "content: Customer ACME prefers deploys on Fridays" in show.stdout
    assert "tags: customer, deploy, priority" in show.stdout
    assert "source: user note" in show.stdout


def test_json_output_and_global_flags_anywhere(db):
    before = as_json("--db", db, "search", "ACME's deploy day")
    after = json.loads(ok("search", "ACME's deploy day", "--db", db, "--json").stdout)
    assert before["hits"] == after["hits"]
    assert before["match_mode"] == "any"
    assert before["hits"][0]["matched_terms"] == ["acme", "deploy"]

    stats = as_json("--db", db, "stats")
    assert stats["total_memories"] == 3
    assert stats["by_namespace"] == {"support": 2, "ops": 1}
    assert stats["schema_version"] == SCHEMA_VERSION

    listing = as_json("--db", db, "list", "--tag", "BILLING")
    assert [m["content"] for m in listing["memories"]] == ["ACME's billing contact is Jane Doe"]


def test_export_import_round_trip_is_identical(db, tmp_path):
    backup = tmp_path / "backup.json"
    ok("--db", db, "export", "-o", str(backup))
    exported = json.loads(backup.read_text(encoding="utf-8"))
    assert exported["count"] == 3

    copy = str(tmp_path / "copy.db")
    result = as_json("--db", copy, "import", str(backup))
    assert (result["imported"], result["skipped"]) == (3, 0)
    again = as_json("--db", copy, "import", str(backup))
    assert (again["imported"], again["skipped"]) == (0, 3)

    def strip(export: dict) -> list[dict]:
        return [{k: v for k, v in m.items() if k != "id"} for m in export["memories"]]

    reexported = json.loads(ok("--db", copy, "export").stdout)  # stdout this time
    assert strip(reexported) == strip(exported)


def test_import_from_stdin_accepts_a_bare_array(tmp_path):
    path = str(tmp_path / "stdin.db")
    payload = json.dumps([{"content": "Piped fact", "tags": ["a"], "created_at": "2026-07-23T05:00:00-07:00"}])
    ok("--db", path, "import", "-", stdin=payload)
    memory = as_json("--db", path, "show", "1")
    assert memory["content"] == "Piped fact"
    assert memory["created_at"] == "2026-07-23T12:00:00Z"


def test_add_reads_content_from_stdin(tmp_path):
    path = str(tmp_path / "add.db")
    ok("--db", path, "add", "-", stdin="Fact from a pipe\n")
    assert as_json("--db", path, "show", "1")["content"] == "Fact from a pipe"


def test_edit_and_forget(db):
    edited = as_json("--db", db, "edit", "2", "--remove-tag", "billing", "--add-tag", "finance",
                     "--namespace", "crm", "--source", "CRM sync")
    assert edited["tags"] == ["finance"]
    assert edited["namespace"] == "crm"
    assert set(edited["updated_fields"]) == {"tags", "namespace", "source"}

    clash = run("--db", db, "edit", "2", "--content", "Customer ACME prefers deploys on Fridays", "--namespace", "support")
    assert clash.returncode == 1
    assert "Memory 1 in namespace 'support' already says exactly this" in clash.stderr

    gone = ok("--db", db, "forget", "3")
    assert "Forgot memory 3" in gone.stdout
    partial = run("--db", db, "forget", "1", "99")
    assert partial.returncode == 1
    assert "Forgot memory 1" in partial.stdout
    assert "Memory 99 not found" in partial.stderr
    assert as_json("--db", db, "stats")["total_memories"] == 1


def test_invalid_input_exits_non_zero_with_a_clear_message(db, tmp_path):
    empty = run("--db", db, "add", "   ")
    assert empty.returncode == 1 and "content must not be empty" in empty.stderr

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("not json", encoding="utf-8")
    result = run("--db", db, "import", str(bad_json))
    assert result.returncode == 1 and "is not valid JSON" in result.stderr

    bad_item = tmp_path / "bad_item.json"
    bad_item.write_text(json.dumps({"memories": [{"content": "x", "expires_at": "soon"}]}), encoding="utf-8")
    result = run("--db", db, "import", str(bad_item))
    assert result.returncode == 1 and "Item 0 has invalid 'expires_at'" in result.stderr

    missing = run("--db", db, "show", "404", "--json")
    assert missing.returncode == 1
    assert "Memory 404 not found" in json.loads(missing.stdout)["error"]

    usage = run("--db", db, "search", "acme", "--match", "fuzzy")
    assert usage.returncode == 2 and "invalid choice" in usage.stderr


def test_doctor_reports_a_healthy_setup(db):
    report = as_json("--db", db, "doctor")
    checks = {c["name"]: c for c in report["checks"]}
    assert report["ok"] is True
    assert checks["fts5"]["status"] == "ok"
    assert checks["schema"]["detail"] == f"v{SCHEMA_VERSION} (current)"
    assert checks["search index"]["status"] == "ok"
    assert "3 memories" in checks["database"]["detail"]
    human = ok("--db", db, "doctor").stdout
    assert "All good." in human


def test_doctor_inspects_an_old_vault_without_touching_it(tmp_path):
    path = tmp_path / "v010.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, "
        "namespace TEXT NOT NULL DEFAULT 'default', tags TEXT NOT NULL DEFAULT '[]', "
        "source TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, expires_at TEXT)"
    )
    conn.execute("INSERT INTO memories (content, created_at) VALUES ('old fact', '2026-07-01T00:00:00Z')")
    conn.commit()
    conn.close()

    report = as_json("--db", str(path), "doctor")
    checks = {c["name"]: c for c in report["checks"]}
    assert checks["schema"]["status"] == "warn"
    assert "upgraded in place" in checks["schema"]["detail"]

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0  # not migrated by doctor
    conn.close()


def test_doctor_fails_on_a_newer_schema(tmp_path):
    path = tmp_path / "future.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    result = run("--db", str(path), "doctor")
    assert result.returncode == 1
    assert "[fail] schema" in result.stdout


def test_cli_works_without_mcp_and_doctor_says_so(tmp_path):
    path = str(tmp_path / "nomcp.db")
    ok("--db", path, "add", "No SDK needed for this", without_mcp=True)
    found = ok("--db", path, "search", "sdk", "--json", without_mcp=True)
    assert json.loads(found.stdout)["count"] == 1

    doctor = run("--db", path, "doctor", without_mcp=True)
    assert doctor.returncode == 1
    assert "[fail] mcp" in doctor.stdout and "not installed" in doctor.stdout

    serve = run("--db", path, "serve", without_mcp=True)
    assert serve.returncode == 2
    assert "needs the 'mcp' package" in serve.stderr


def test_version_flag():
    from mcp_memory_vault import __version__

    assert ok("--version").stdout.strip() == f"mcp-memory-vault {__version__}"
