#!/usr/bin/env python3
"""Exercise real local embeddings against newly authored multilingual fixtures."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from codex_mem.semantic import (
    DIMENSIONS,
    MAX_CHUNK_TOKENS,
    MODEL,
    MODEL_REVISION,
    PASSAGE_PREFIX,
    QUERY_PREFIX,
    FastEmbedBackend,
    index_pending,
    search,
    semantic_status,
)
from codex_mem.store import Store, project_key

CASES = [
    ("The database password changes each night.", "Credential rotation schedule"),
    ("Платежи отклонялись из-за истёкшего сертификата.", "Expired TLS certificate caused transaction failures"),
    ("The queue automatically retries failed jobs with increasing delays.", "Повторная обработка задач после ошибок с растущей паузой"),
]


def _ranked_results(result):
    return [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "semantic_score": item.get("semantic_score"),
        }
        for item in result.get("results", [])
    ]


def _rank(result, target):
    for position, item in enumerate(result.get("results", []), start=1):
        if item.get("id") == target:
            return position
    return None


def _actual_long_tail_check():
    """Prove the installed tokenizer preserves tails within its true context."""

    backend = FastEmbedBackend()
    planner = backend._get_planner_tokenizer()
    checks = {}
    for name, text, prefix, marker in (
        ("document", ("x-/-" * 200) + " document-tail", PASSAGE_PREFIX, "document-tail"),
        ("query", ("x-/-" * 245) + " query-tail", QUERY_PREFIX, "query-tail"),
    ):
        chunks = backend.split(text, prefix=prefix)
        token_counts = [
            len(planner.encode(chunk, add_special_tokens=True).ids) for chunk in chunks
        ]
        assert len(chunks) > 1, (name, len(chunks), token_counts)
        assert "".join(chunk.removeprefix(prefix) for chunk in chunks) == text
        assert marker in chunks[-1]
        assert max(token_counts) <= MAX_CHUNK_TOKENS, token_counts
        checks[name] = {
            "source_chars": len(text),
            "chunks": len(chunks),
            "max_total_tokens": max(token_counts),
            "tail_preserved": True,
        }
    return checks


def run():
    started = time.monotonic()
    receipt = {
        "status": "failed",
        "model": semantic_status(),
        "data": "new fictional-only temporary database",
        "inference": "local CPU; no memory text sent to an external service",
        "cases": [],
        "quality": {
            "semantic_top1_count": 0,
            "hybrid_top1_count": 0,
            "semantic_ranks": [],
            "hybrid_ranks": [],
        },
    }
    try:
        assert receipt["model"]["status"] == "ready", receipt["model"]
        receipt["tokenizer_long_tail"] = _actual_long_tail_check()
        with tempfile.TemporaryDirectory(prefix="codex-mem-semantic-acceptance-") as temporary:
            base = Path(temporary)
            project, other = base / "project", base / "other"
            project.mkdir(); other.mkdir()
            workspace = project_key(project)
            data = base / "memory"
            with Store(data) as store:
                ids = [store.remember(project, f"Fixture {i}", body)["id"] for i, (body, _) in enumerate(CASES)]
                for i, body in enumerate(["The camera records high resolution videos.",
                                         "The hotel serves a continental breakfast.",
                                         "Customers receive refunds when they cancel their reservations."]):
                    store.remember(project, f"Distractor {i}", body)
                store.remember(other, "Private fixture", CASES[0][0])
                lexical = [store.search(project, query) for _, query in CASES]
                assert all(not values for values in lexical), "Fixtures must have no lexical query overlap"
            indexed = index_pending(project, data)
            receipt["index"] = indexed
            assert indexed["status"] == "indexed" and indexed["indexed"] == 6, indexed
            assert index_pending(project, data)["status"] == "idle"
            with Store(data) as store:
                for target, (body, query) in zip(ids, CASES):
                    case = {"stored": body, "query": query, "lexical_matches": 0}
                    result = search(store, project, query, mode="semantic", limit=3)
                    case["semantic_results"] = _ranked_results(result)
                    semantic_rank = _rank(result, target)
                    case["semantic_rank"] = semantic_rank
                    case["semantic_top1"] = semantic_rank == 1
                    receipt["quality"]["semantic_ranks"].append(semantic_rank)
                    if semantic_rank == 1:
                        receipt["quality"]["semantic_top1_count"] += 1
                    if semantic_rank != 1:
                        case["status"] = "failed"
                        receipt["cases"].append(case)
                        raise AssertionError(f"semantic rank failed for query {query!r}: {case['semantic_results']!r}")
                    assert all(r["project"] == workspace for r in result["results"]), result["results"]
                    hybrid = search(store, project, query, mode="hybrid", limit=3)
                    case["hybrid_results"] = _ranked_results(hybrid)
                    hybrid_rank = _rank(hybrid, target)
                    case["hybrid_rank"] = hybrid_rank
                    case["hybrid_top1"] = hybrid_rank == 1
                    receipt["quality"]["hybrid_ranks"].append(hybrid_rank)
                    if hybrid_rank == 1:
                        receipt["quality"]["hybrid_top1_count"] += 1
                    if hybrid_rank != 1:
                        case["status"] = "failed"
                        receipt["cases"].append(case)
                        raise AssertionError(f"hybrid rank failed for query {query!r}: {case['hybrid_results']!r}")
                    case.update(
                        status="passed",
                        score=result["results"][0].get("semantic_score"),
                    )
                    receipt["cases"].append(case)
                assert search(store, other, CASES[0][1], mode="semantic")["results"] == []
                store.forget(project, [ids[0]])
                assert all(r["id"] != ids[0] for r in search(store, project, CASES[0][1], mode="semantic")["results"])
                summary = store.remember(project, "Updated conclusion", CASES[1][0], source_ids=[ids[1]])
                assert all(r["id"] != ids[1] for r in search(store, project, CASES[1][1], mode="semantic")["results"])
            assert index_pending(project, data)["status"] == "indexed"
            with Store(data) as store:
                assert search(store, project, CASES[1][1], mode="semantic")["results"][0]["id"] == summary["id"]
                receipt["index_status"] = store.embedding_status(
                    project, model=MODEL, revision=MODEL_REVISION, dimensions=DIMENSIONS
                )
                assert receipt["index_status"]["pending"] == 0, receipt["index_status"]
            receipt.update(status="passed", dimensions=DIMENSIONS,
                           checks=["real multilingual paraphrase ranking", "zero lexical overlap", "hybrid ranking",
                                   "project isolation", "delete removes retrieval", "supersession removes retrieval",
                                   "new summary indexed", "idle repeat", "reopen retrieval",
                                   "actual tokenizer tail preservation"])
    except Exception as exc:
        receipt.update(status="failed", error=type(exc).__name__, detail=str(exc)[:3_000])
    finally:
        receipt["temporary_database_removed"] = True
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run()
    except Exception as exc:
        result = {"status": "failed", "error": type(exc).__name__, "detail": str(exc)[:3000]}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)
