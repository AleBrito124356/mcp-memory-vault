"""Recall quality: the questions agents actually ask must find the fact.

v0.1.0 ANDed every raw word of the query, so "when does ACME prefer to
deploy" required "when", "does" and "to" to appear in the stored fact and
returned nothing. The table below is a regression suite of natural-language,
possessive, punctuated, prefix and Spanish queries with the expected best
hit, checked in both search modes (FTS5 and the LIKE fallback).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_memory_vault.core import MemoryVault, query_terms  # noqa: E402

CORPUS = [
    "Customer ACME prefers deploys on Fridays",
    "ACME's billing contact is Jane Doe (jane@acme.example)",
    "Globex staging database resets nightly at 02:00 UTC",
    "Initech invoices are due net-30",
    "Umbrella wants weekly status emails every Monday",
    "Stark Industries runs Kubernetes on-prem",
    "Wayne Enterprises signed the annual support contract in March",
    "Hooli rate limits their public API at 100 requests per second",
    "Pied Piper compression benchmarks run on Tuesdays",
    "Vandelay imports go through the EU region",
    "The user prefers dark mode and vim keybindings",
    "The user's preferred language for code reviews is Spanish",
    "Project Atlas uses Postgres 16 with pgvector",
    "Project Borealis is written in Rust and deployed with Nomad",
    "The CI pipeline runs on GitHub Actions with a 20 minute timeout",
    "Production deployments are frozen during the last week of December",
    "Staging credentials live in the team password manager, never in the repo",
    "Maria is the on-call engineer for payments this week",
    "The payments service retries failed webhooks 5 times with exponential backoff",
    "Customer Globex asked for SSO via Okta",
    "Weekly sync with the design team is on Thursdays at 10:00",
    "The mobile app release train ships every two weeks",
    "Error budget for the search API is 99.9% availability",
    "El cliente Soylent prefiere reuniones por la mañana",
    "La base de datos de producción se respalda cada noche a las 3:00",
    "Logs are retained for 30 days in Loki",
    "The frontend is built with Next.js and deployed on Vercel",
    "Feature flags are managed in LaunchDarkly",
    "Invoices for ACME must be sent to accounts payable, not to Jane",
    "The user dislikes long meetings and prefers async updates",
]

# (query, expected top hit)
QUERIES = [
    ("when does ACME prefer to deploy", CORPUS[0]),
    ("ACME's deploy day", CORPUS[0]),
    ("deploy day for acme?", CORPUS[0]),
    ("who is ACME's billing contact?", CORPUS[1]),
    ("what database does project Atlas use", CORPUS[12]),
    ("Postgres version for Atlas?", CORPUS[12]),
    ("who's on call for payments", CORPUS[17]),
    ("kube", CORPUS[5]),
    ("invoice due date for initech", CORPUS[3]),
    ("¿Qué prefiere el cliente Soylent?", CORPUS[23]),
    ("dark-mode", CORPUS[10]),
    ("GitHub Actions timeout?", CORPUS[14]),
    ("SSO provider for Globex", CORPUS[19]),
    ("retention of logs", CORPUS[25]),
    ("weekly design sync", CORPUS[20]),
    ("payment webhooks retry", CORPUS[18]),
    ("vercel deploys?", CORPUS[26]),
]

# Queries from the bug report: each returned 0 hits in v0.1.0.
REPORTED = ["when does ACME prefer to deploy", "ACME's deploy day", "deploy day for acme?"]


@pytest.fixture(params=["fts5", "like"])
def seeded(request, tmp_path, monkeypatch):
    vault = MemoryVault(db_path=tmp_path / "quality.db")
    for fact in CORPUS:
        vault.remember(fact)
    if request.param == "like":
        monkeypatch.setattr(vault, "fts5_available", False)
    yield vault
    vault.close()


@pytest.mark.parametrize("query,expected", QUERIES, ids=[q for q, _ in QUERIES])
def test_natural_language_queries_find_the_fact(seeded, query, expected):
    result = seeded.recall(query)
    assert result["count"] >= 1, f"{query!r} found nothing ({seeded.search_mode})"
    assert result["hits"][0]["content"] == expected, (
        f"{query!r} ({seeded.search_mode}, {result['match_mode']}) ranked "
        f"{[h['content'] for h in result['hits'][:3]]}"
    )


def test_the_reported_queries_now_hit(seeded):
    for query in REPORTED + ["ACME deploy"]:
        assert seeded.recall(query)["hits"][0]["content"] == CORPUS[0], query


def test_query_normalisation():
    assert [t.text for t in query_terms("When does ACME's team deploy?")] == [
        "acme",
        "team",
        "deploy",
    ]
    assert [t.text for t in query_terms("¿Cuándo prefiere el cliente?")] == ["prefiere", "cliente"]
    # Only stopwords: search them rather than nothing.
    assert [t.text for t in query_terms("The Who")] == ["the", "who"]
    deploy = query_terms("deploys")[0]
    assert (deploy.text, deploy.root, deploy.prefix) == ("deploys", "deplo", True)
    assert query_terms("C++ 100")[1].prefix is False  # numbers match exactly
    assert query_terms("%") == []
    assert len(query_terms(" ".join(f"word{i}" for i in range(40)))) == 12


def test_auto_reports_a_partial_match_honestly(seeded):
    result = seeded.recall("ACME's deploy day")
    assert result["terms"] == ["acme", "deploy", "day"]
    if seeded.search_mode == "fts5":
        # No fact contains all three words, so auto fell back to "any".
        assert result["match_mode"] == "any"
        assert "partial matches" in result["note"]
        assert result["hits"][0]["matched_terms"] == ["acme", "deploy"]


def test_match_all_keeps_the_strict_semantics(seeded):
    strict = seeded.recall("ACME deploy", match="all")
    assert strict["match_mode"] == "all"
    assert [h["content"] for h in strict["hits"]] == [CORPUS[0]]
    assert strict["hits"][0]["matched_terms"] == ["acme", "deploy"]
    # A term that no fact contains means no hits in strict mode.
    assert seeded.recall("ACME deploy zeppelin", match="all")["count"] == 0
    assert seeded.recall("ACME deploy zeppelin")["hits"][0]["content"] == CORPUS[0]


def test_match_any_ranks_by_terms_matched(seeded):
    result = seeded.recall("acme invoices jane", match="any", limit=5)
    assert result["match_mode"] == "any"
    top = result["hits"][0]
    assert top["content"] == CORPUS[28]  # the only fact with all three words
    assert top["matched_terms"] == ["acme", "invoices", "jane"]
    assert all(h["matched_terms"] for h in result["hits"])
    counts = [len(h["matched_terms"]) for h in result["hits"]]
    assert counts == sorted(counts, reverse=True)


def test_filters_and_limit_apply_in_every_mode(tmp_path):
    with MemoryVault(db_path=tmp_path / "filters.db") as vault:
        for i in range(30):
            vault.remember(f"deploy note {i}", namespace="ops" if i % 2 else "dev", tags=["deploy"] if i % 3 == 0 else [])
        for match in ("all", "any", "auto"):
            result = vault.recall("deploy note", namespace="ops", tags=["DEPLOY"], limit=3, match=match)
            assert result["count"] == 3
            for hit in result["hits"]:
                assert hit["namespace"] == "ops" and hit["tags"] == ["deploy"]


def test_invalid_match_and_empty_results_explain_themselves(seeded):
    with pytest.raises(ValueError, match="match must be one of auto, all, any"):
        seeded.recall("acme", match="fuzzy")
    nothing = seeded.recall("zeppelin")
    assert nothing["count"] == 0
    assert "Try fewer or different words" in nothing["hint"]
