"""Deterministic contracts for the optional local semantic layer."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_mem import semantic


def _vector(first: float = 3.0, second: float = 4.0) -> list[float]:
    return [first, second] + [0.0] * (semantic.DIMENSIONS - 2)


class FakeEncoding:
    def __init__(self, ids: list[int], offsets: list[tuple[int, int]]) -> None:
        self.ids = ids
        self.offsets = offsets


class CharacterPieceTokenizer:
    """A deterministic wordpiece-like tokenizer for offset-boundary tests."""

    def encode(self, text: str, *, add_special_tokens: bool) -> FakeEncoding:
        ids = list(range(len(text)))
        if add_special_tokens:
            ids = [-1, *ids, -2]
        return FakeEncoding(ids, [(index, index + 1) for index in range(len(text))])


class FakeBackend:
    def __init__(self, *, ready: bool = True, code: str | None = None) -> None:
        self._ready = ready
        self._code = code
        self.calls: list[list[str]] = []
        self.tokenizer = CharacterPieceTokenizer()

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def unavailable_code(self) -> str | None:
        return self._code

    def split(self, text: str, *, prefix: str = "") -> list[str]:
        return semantic._split_with_tokenizer(self.tokenizer, text, prefix=prefix)

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [_vector() for _ in texts]


class FakeSearchStore:
    def __init__(self) -> None:
        self.lexical_calls: list[tuple[str, str, int, object, object, object, object]] = []
        self.semantic_calls: list[
            tuple[str, list[float], str, str, int, int, object, object, object, object]
        ] = []
        self.lexical_results: list[dict[str, object]] = []
        self.semantic_results: list[dict[str, object]] = []

    def search(
        self,
        project: str,
        query: str,
        *,
        limit: int,
        kinds: object = None,
        types: object = None,
        concepts: object = None,
        files: object = None,
    ) -> list[dict[str, object]]:
        self.lexical_calls.append((project, query, limit, kinds, types, concepts, files))
        return self.lexical_results

    def semantic_search(
        self,
        project: str,
        query_vector: list[float],
        model: str,
        revision: str,
        dimensions: int,
        *,
        limit: int,
        kinds: object = None,
        types: object = None,
        concepts: object = None,
        files: object = None,
    ) -> list[dict[str, object]]:
        self.semantic_calls.append(
            (project, query_vector, model, revision, dimensions, limit, kinds, types, concepts, files)
        )
        return self.semantic_results


class FakeIndexStore:
    instances: list["FakeIndexStore"] = []
    claim: dict[str, object] | None = None

    def __init__(self, _data_dir: object = None) -> None:
        self.claim_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.completed: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.failed: list[tuple[object, ...]] = []
        FakeIndexStore.instances.append(self)

    def __enter__(self) -> "FakeIndexStore":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def claim_embedding_batch(self, *args: object, **kwargs: object) -> dict[str, object] | None:
        self.claim_calls.append((args, kwargs))
        return self.claim

    def complete_embedding_batch(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.completed.append((args, kwargs))
        return {"indexed_count": len(kwargs["vectors"])}

    def fail_embedding_batch(self, *args: object) -> None:
        self.failed.append(args)

    def embedding_status(self, *_args: object) -> dict[str, int]:
        return {"pending": 0}


class SemanticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_auto_falls_back_to_lexical_without_claiming_semantic_ready(self) -> None:
        store = FakeSearchStore()
        store.lexical_results = [{"id": "lexical-a", "title": "Lexical match"}]
        result = semantic.search(
            store,
            self.project,
            "token query",
            mode="auto",
            backend=FakeBackend(ready=False, code="model_not_ready"),
        )

        self.assertEqual("auto", result["requested_mode"])
        self.assertEqual("lexical", result["used_mode"])
        self.assertEqual("model_not_ready", result["fallback_reason"])
        self.assertEqual(["lexical-a"], [record["id"] for record in result["results"]])
        self.assertEqual(1, len(store.lexical_calls))
        self.assertEqual([], store.semantic_calls)

    def test_resume_reorders_bounded_relevant_candidates_in_every_mode(self) -> None:
        candidates = [
            {"id": "ui", "kind": "note"},
            {"id": "raw", "kind": "tool"},
            {"id": "decision", "kind": "note", "observation": {"type": "decision"}},
            {"id": "summary", "kind": "session_summary"},
            {"id": "fix", "kind": "bugfix"},
            {"id": "another-summary", "kind": "session_summary"},
        ]
        for mode, ready in (("lexical", True), ("semantic", True), ("hybrid", True), ("auto", False)):
            with self.subTest(mode=mode):
                store = FakeSearchStore()
                store.lexical_results = candidates
                store.semantic_results = candidates
                result = semantic.search(
                    store, self.project, "roadmap", mode=mode, intent="resume", limit=3,
                    backend=FakeBackend(ready=ready),
                )
                self.assertEqual("resume", result["intent"])
                self.assertEqual(["summary", "decision", "another-summary"],
                                 [record["id"] for record in result["results"]])
                for call in store.lexical_calls:
                    self.assertEqual(12 if call[3] is None and call[4] is None else 3, call[2])
                for call in store.semantic_calls:
                    self.assertEqual(12, call[5])

    def test_resume_expands_priority_categories_across_search_modes(self) -> None:
        class FilteredStore(FakeSearchStore):
            def search(self, project, query, *, limit, kinds=None, types=None, concepts=None, files=None):
                candidates = super().search(project, query, limit=limit, kinds=kinds, types=types,
                                            concepts=concepts, files=files)
                return [record for record in candidates
                        if query in record["title"]
                        and (kinds is None or record.get("kind") in kinds)
                        and (types is None or record.get("observation", {}).get("type") in types)][:limit]

            def semantic_search(self, *args, **kwargs):
                return super().semantic_search(*args, **kwargs)[:kwargs["limit"]]

        details = [{"id": f"ui-{index:02}", "title": "roadmap theme", "kind": "note"}
                   for index in range(35)]
        handoffs = [
            {"id": "summary", "title": "roadmap handoff", "kind": "session_summary", "score": 2.5},
            {"id": "decision", "title": "roadmap decision", "kind": "note", "observation": {"type": "decision"}},
            {"id": "unrelated", "title": "billing handoff", "kind": "session_summary"},
        ]
        for mode, ready in (("lexical", True), ("semantic", True), ("hybrid", True), ("auto", True), ("auto", False)):
            with self.subTest(mode=mode, ready=ready):
                store = FilteredStore()
                store.lexical_results = details + handoffs
                store.semantic_results = details + handoffs
                result = semantic.search(store, self.project, "roadmap", mode=mode, intent="resume", limit=5,
                                         backend=FakeBackend(ready=ready))
                self.assertEqual(["summary", "decision"], [item["id"] for item in result["results"][:2]])
                self.assertNotIn("unrelated", [item["id"] for item in result["results"]])
                self.assertEqual(mode, result["requested_mode"])
                self.assertEqual("lexical", result["resume_expansion_mode"])
                self.assertEqual(2, result["resume_added_candidates"])
                self.assertEqual("hybrid" if ready and mode != "lexical" else "lexical", result["used_mode"])
                summary = result["results"][0]
                if result["used_mode"] == "hybrid":
                    self.assertEqual(2.5, summary["lexical_score"])
                    self.assertNotIn("score", summary)
                else:
                    self.assertEqual(2.5, summary["score"])

    def test_resume_keeps_explicit_semantic_model_requirement(self) -> None:
        store = FakeSearchStore()
        store.lexical_results = [{"id": "summary", "kind": "session_summary"}]
        for mode in ("semantic", "hybrid"):
            with self.subTest(mode=mode), self.assertRaisesRegex(semantic.SemanticError, "model_not_ready"):
                semantic.search(store, self.project, "roadmap", mode=mode, intent="resume",
                                backend=FakeBackend(ready=False, code="model_not_ready"))
        self.assertEqual([], store.lexical_calls)

    def test_resume_balances_summaries_and_keeps_one_match_per_session(self) -> None:
        candidates = [
            {"id": f"summary-{index}", "kind": "session_summary", "session_id": "same-chat"}
            for index in range(6)
        ] + [
            {"id": "other-summary", "kind": "session_summary", "session_id": "other-chat"},
            {"id": "decision", "kind": "decision"},
            {"id": "discovery", "kind": "discovery"},
        ]
        for mode, ready in (("lexical", True), ("semantic", True), ("hybrid", True), ("auto", False)):
            with self.subTest(mode=mode):
                store = FakeSearchStore()
                store.lexical_results = candidates
                store.semantic_results = candidates
                result = semantic.search(store, self.project, "roadmap", mode=mode,
                                         intent="resume", limit=4, backend=FakeBackend(ready=ready))
                self.assertEqual(["summary-0", "decision", "other-summary", "discovery"],
                                 [record["id"] for record in result["results"]])
                lookup = semantic.search(store, self.project, "roadmap", mode="lexical", limit=20)
                self.assertEqual(candidates, lookup["results"])

    def test_resume_preserves_relevance_within_tiers_and_keeps_history(self) -> None:
        store = FakeSearchStore()
        store.lexical_results = [
            {"id": "tool", "kind": "tool"},
            {"id": "note-one", "kind": "note"},
            {"id": "alert", "kind": "note", "observation": {"type": "security_alert"}},
            {"id": "note-two", "kind": "discovery"},
            {"id": "session", "kind": "session"},
            {"id": "checkpoint", "kind": "checkpoint"},
        ]
        lookup = semantic.search(store, self.project, "roadmap", mode="lexical", limit=6)
        resumed = semantic.search(store, self.project, "roadmap", mode="lexical", intent="resume", limit=6)
        self.assertEqual(store.lexical_results, lookup["results"])
        self.assertEqual(["alert", "note-one", "note-two", "tool", "session", "checkpoint"],
                         [record["id"] for record in resumed["results"]])
        self.assertEqual({record["id"] for record in lookup["results"]},
                         {record["id"] for record in resumed["results"]})

    def test_resume_candidate_limit_is_capped_and_invalid_intent_fails(self) -> None:
        store = FakeSearchStore()
        semantic.search(store, self.project, "roadmap", mode="lexical", intent="resume", limit=50)
        self.assertEqual(100, store.lexical_calls[0][2])
        for intent in ("current", [], None):
            with self.subTest(intent=intent), self.assertRaisesRegex(ValueError, "intent"):
                semantic.search(store, self.project, "roadmap", mode="lexical", intent=intent)

    def test_explicit_semantic_modes_fail_when_the_model_is_unavailable(self) -> None:
        store = FakeSearchStore()
        backend = FakeBackend(ready=False, code="dependency_missing")

        for mode in ("semantic", "hybrid"):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(semantic.SemanticError, "dependency_missing"):
                    semantic.search(store, self.project, "query", mode=mode, backend=backend)

    def test_semantic_query_is_redacted_prefixed_and_l2_normalized(self) -> None:
        store = FakeSearchStore()
        store.semantic_results = [{"id": "semantic-a", "score": 0.9}]
        backend = FakeBackend()

        result = semantic.search(
            store,
            self.project,
            "DATABASE_PASSWORD=hunter2 multilingual search",
            mode="semantic",
            backend=backend,
        )

        self.assertEqual("semantic", result["used_mode"])
        encoded = backend.calls[0]
        self.assertEqual(1, len(encoded))
        self.assertTrue(encoded[0].startswith(semantic.QUERY_PREFIX))
        self.assertNotIn("hunter2", encoded[0])
        self.assertIn("[REDACTED]", encoded[0])
        vector = store.semantic_calls[0][1]
        self.assertAlmostEqual(0.6, vector[0])
        self.assertAlmostEqual(0.8, vector[1])
        self.assertAlmostEqual(1.0, sum(value * value for value in vector))

    def test_hybrid_uses_rrf_and_stable_entry_ids_for_ties(self) -> None:
        store = FakeSearchStore()
        store.lexical_results = [
            {"id": "a", "title": "A"},
            {"id": "b", "title": "B"},
        ]
        store.semantic_results = [
            {"id": "b", "title": "B semantic"},
            {"id": "c", "title": "C"},
        ]

        result = semantic.search(store, self.project, "query", mode="hybrid", backend=FakeBackend())

        self.assertEqual(["b", "a", "c"], [record["id"] for record in result["results"]])
        self.assertEqual("hybrid", result["used_mode"])
        self.assertGreater(result["results"][0]["score"], result["results"][1]["score"])

    def test_structured_filters_reach_both_candidate_queries_before_rrf_limit(self) -> None:
        store = FakeSearchStore()
        store.lexical_results = [{"id": "lexical-a", "title": "A"}]
        store.semantic_results = [{"id": "semantic-a", "title": "A"}]

        semantic.search(
            store,
            self.project,
            "query",
            mode="hybrid",
            limit=2,
            kinds=["note"],
            types=["bugfix"],
            concepts=["sqlite"],
            files=["codex_mem/store.py"],
            backend=FakeBackend(),
        )

        lexical = store.lexical_calls[0]
        semantic_call = store.semantic_calls[0]
        self.assertEqual(8, lexical[2])
        self.assertEqual(8, semantic_call[5])
        self.assertEqual(("note",), tuple(lexical[3]))
        self.assertEqual(("bugfix",), tuple(lexical[4]))
        self.assertEqual(("sqlite",), tuple(lexical[5]))
        self.assertEqual(("codex_mem/store.py",), tuple(lexical[6]))
        self.assertEqual(lexical[3:], semantic_call[6:])

    def test_tokenizer_offsets_chunk_long_russian_wordpieces_without_losing_the_tail(self) -> None:
        tokenizer = CharacterPieceTokenizer()
        text = ("сверхдлинноеслово" * 40) + " хвост-поиска"

        chunks = semantic._split_with_tokenizer(tokenizer, text)

        self.assertGreater(len(chunks), 1)
        self.assertEqual(text, "".join(chunks))
        self.assertIn("хвост-поиска", chunks[-1])
        self.assertTrue(
            all(
                len(tokenizer.encode(chunk, add_special_tokens=True).ids)
                <= semantic.MAX_CHUNK_TOKENS
                for chunk in chunks
            )
        )

    def test_e5_prefix_is_repeated_and_reserved_in_each_chunk(self) -> None:
        tokenizer = CharacterPieceTokenizer()
        text = ("сверхдлинноеслово" * 40) + " хвост-поиска"

        chunks = semantic._split_with_tokenizer(
            tokenizer, text, prefix=semantic.PASSAGE_PREFIX
        )

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.startswith(semantic.PASSAGE_PREFIX) for chunk in chunks))
        self.assertEqual(
            text,
            "".join(chunk.removeprefix(semantic.PASSAGE_PREFIX) for chunk in chunks),
        )
        self.assertTrue(
            all(
                len(tokenizer.encode(chunk, add_special_tokens=True).ids)
                <= semantic.MAX_CHUNK_TOKENS
                for chunk in chunks
            )
        )

    def test_long_query_uses_all_tokenizer_chunks_including_its_tail(self) -> None:
        store = FakeSearchStore()
        store.semantic_results = [{"id": "semantic-a", "score": 0.9}]
        backend = FakeBackend()
        query = ("длинныйзапрос" * 70) + " query-tail"

        semantic.search(store, self.project, query, mode="semantic", backend=backend)

        self.assertGreater(sum(len(call) for call in backend.calls), 1)
        flattened = "".join(
            text.removeprefix(semantic.QUERY_PREFIX)
            for call in backend.calls
            for text in call
        )
        self.assertIn("query-tail", flattened)

    def test_index_claims_one_bounded_batch_and_keeps_the_last_chunk(self) -> None:
        FakeIndexStore.instances.clear()
        text = "canonical embedding text " + ("слово" * 160) + " tail-marker."
        FakeIndexStore.claim = {
            "job_id": "job-1",
            "lease_token": "lease-1",
            "entries": [
                {
                    "id": "entry-1",
                    "text": text,
                    "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            ],
        }
        backend = FakeBackend()
        with patch("codex_mem.semantic.Store", FakeIndexStore):
            result = semantic.index_pending(self.project, self.root / "data", backend=backend)

        self.assertEqual({"status": "indexed", "indexed": 1, "pending": 0}, result)
        store = FakeIndexStore.instances[-1]
        claimed_args, claimed_kwargs = store.claim_calls[0]
        self.assertEqual(semantic.MODEL, claimed_args[1])
        self.assertEqual(semantic.MODEL_REVISION, claimed_args[2])
        self.assertEqual(semantic.DIMENSIONS, claimed_args[3])
        self.assertEqual(semantic.INDEX_BATCH_SIZE, claimed_kwargs["limit"])
        self.assertEqual(semantic.MAX_DOCUMENT_CHARS, claimed_kwargs["max_chars"])
        flattened = " ".join(text for call in backend.calls for text in call)
        self.assertTrue(all(text.startswith(semantic.PASSAGE_PREFIX) for call in backend.calls for text in call))
        self.assertIn("tail-marker.", flattened)
        vectors = store.completed[0][1]["vectors"]
        self.assertEqual("entry-1", vectors[0]["entry_id"])
        self.assertAlmostEqual(0.6, vectors[0]["vector"][0])
        self.assertAlmostEqual(0.8, vectors[0]["vector"][1])

    def test_unavailable_indexer_leaves_the_queue_unclaimed(self) -> None:
        with patch("codex_mem.semantic.Store", side_effect=AssertionError("must not open Store")):
            result = semantic.index_pending(
                self.project,
                backend=FakeBackend(ready=False, code="model_not_ready"),
            )

        self.assertEqual(
            {"status": "unavailable", "indexed": 0, "pending": 0, "code": "model_not_ready"},
            result,
        )

    def test_status_is_offline_and_rejects_an_unverified_model_directory(self) -> None:
        status = semantic.semantic_status(self.root / "unprepared-model")

        self.assertEqual("unavailable", status["status"])
        self.assertEqual("model_not_ready", status["code"])
        self.assertEqual(semantic.MODEL_REVISION, status["revision"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
