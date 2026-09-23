"""One MemoryVault shared by many threads must neither crash nor lose writes.

mcp 2.x runs synchronous tools on worker threads, so the server's single
vault is hit concurrently. Before the lock this produced InterfaceError /
SystemError and silently dropped most of the writes.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_memory_vault.core import MemoryVault  # noqa: E402

THREADS = 8
OPS = 150


def test_threads_share_one_vault_without_errors_or_lost_writes(tmp_path):
    vault = MemoryVault(db_path=tmp_path / "threads.db")
    errors: list[str] = []
    start = threading.Barrier(THREADS)

    def worker(n: int) -> None:
        start.wait()
        try:
            for i in range(OPS):
                vault.remember(f"thread {n} fact {i} about widgets", namespace=f"t{n}")
                vault.recall("widgets", limit=3)
                vault.list_memories(limit=5)
        except Exception as exc:  # pragma: no cover - the assertion reports it
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert errors == []
        stats = vault.memory_stats()
        assert stats["total_memories"] == THREADS * OPS
        assert stats["by_namespace"] == {f"t{n}": OPS for n in range(THREADS)}
    finally:
        vault.close()


def test_two_vaults_on_one_file_interleave_safely(tmp_path):
    """Two processes (e.g. two agents) use separate connections to one file."""
    path = tmp_path / "shared.db"
    a, b = MemoryVault(db_path=path), MemoryVault(db_path=path)
    errors: list[str] = []

    def writer(vault: MemoryVault, label: str) -> None:
        try:
            for i in range(100):
                vault.remember(f"{label} note {i}")
        except Exception as exc:  # pragma: no cover
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [
        threading.Thread(target=writer, args=(a, "alpha")),
        threading.Thread(target=writer, args=(b, "beta")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    try:
        assert errors == []
        assert a.memory_stats()["total_memories"] == 200
        assert b.recall("beta note")["count"] > 0
    finally:
        a.close()
        b.close()
