"""Queue counts use persisted metadata; full integrity checks remain explicit."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codex_mem import semantic
from codex_mem.store import Store, StoreError


class EmbeddingQueueCountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.foreign = self.root / "foreign"
        self.project.mkdir()
        self.foreign.mkdir()
        self.store = Store(self.root / "data")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def count(self) -> int:
        return self.store.pending_embedding_count(self.project, "test-model", "r1", 2)

    def index(self) -> None:
        batch = self.store.claim_embedding_batch(self.project, "test-model", "r1", 2)
        assert batch is not None
        self.store.complete_embedding_batch(
            self.project, batch["job_id"], batch["lease_token"],
            vectors=[{"entry_id": item["id"], "content_hash": item["content_hash"],
                      "vector": [1.0, 0.0]} for item in batch["entries"]],
        )

    def test_empty_missing_and_current_embeddings(self) -> None:
        self.assertEqual(0, self.count())
        self.store.remember(self.project, "Decision", "Keep the whole document.")
        self.assertEqual(1, self.count())
        self.index()
        self.assertEqual(0, self.count())

    def test_missing_document_outdated_version_and_stale_hashes(self) -> None:
        mutations = [
            "DELETE FROM embedding_documents WHERE entry_id = ?",
            "UPDATE embedding_documents SET text_version = 'v1' WHERE entry_id = ?",
            "UPDATE embedding_documents SET content_hash = '" + "a" * 64 + "' WHERE entry_id = ?",
            "UPDATE embedding_vectors SET content_hash = '" + "b" * 64 + "' WHERE entry_id = ?",
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                entry = self.store.remember(self.project, "Metadata", "A new body.")
                self.index()
                self.store._connection.execute(mutation, (entry["id"],))
                self.assertEqual(1, self.count())
                # Remove this fixture without repairing or scanning its content.
                self.store._connection.execute("DELETE FROM entries WHERE id = ?", (entry["id"],))

    def test_other_profiles_and_foreign_cache_rows_do_not_satisfy_count(self) -> None:
        entry = self.store.remember(self.project, "Profile", "A decision.")
        self.index()
        for column, value in [("model", "other"), ("revision", "other"),
                              ("dimensions", 3), ("project", str(self.foreign))]:
            with self.subTest(column=column):
                self.store._connection.execute(
                    f"UPDATE embedding_vectors SET {column} = ? WHERE entry_id = ?", (value, entry["id"])
                )
                self.assertEqual(1, self.count())
                original = {"model": "test-model", "revision": "r1", "dimensions": 2,
                            "project": str(self.project)}[column]
                self.store._connection.execute(
                    f"UPDATE embedding_vectors SET {column} = ? WHERE entry_id = ?", (original, entry["id"])
                )
        self.store._connection.execute(
            "UPDATE embedding_documents SET project = ? WHERE entry_id = ?", (str(self.foreign), entry["id"])
        )
        self.assertEqual(1, self.count())

    def test_excludes_superseded_raw_and_foreign_entries(self) -> None:
        source = self.store.remember(self.project, "Source", "Source decision.")
        self.store.remember(self.project, "Summary", "Consolidated decision.", source_ids=[source["id"]])
        self.store.remember(self.project, "Raw", "Raw event.", kind="tool", source="hook:PostToolUse")
        self.store.remember(self.foreign, "Foreign", "Foreign event.")
        self.assertEqual(1, self.count())
        # GLOB preserves the existing case-sensitive raw-source policy.
        self.store.remember(self.project, "Manual", "Manual tool note.", kind="tool", source="Hook:manual")
        self.assertEqual(2, self.count())

    def test_running_and_failed_jobs_still_count_as_backlog(self) -> None:
        self.store.remember(self.project, "Blocked", "A complete pending document.")
        batch = self.store.claim_embedding_batch(self.project, "test-model", "r1", 2)
        assert batch is not None
        self.assertEqual(1, self.count())
        self.store.fail_embedding_batch(self.project, batch["job_id"], batch["lease_token"], "test_failure")
        self.assertEqual(1, self.count())

    def test_count_reads_no_body_or_vector_and_performs_no_audit(self) -> None:
        self.store.remember(self.project, "Whole document", "DATABASE_PASSWORD=hidden\n" + "tail " * 300)
        self.index()
        statements: list[str] = []
        self.store._connection.set_trace_callback(statements.append)
        with patch.object(Store, "_embedding_text_and_hash_from_row", side_effect=AssertionError("body audit")), \
             patch("codex_mem.store._unpack_embedding_vector", side_effect=AssertionError("vector decode")), \
             patch.object(Store, "embedding_status", side_effect=AssertionError("full audit")):
            self.assertEqual(0, self.count())
        self.store._connection.set_trace_callback(None)
        self.assertEqual(1, len(statements))
        query = statements[0].lower()
        self.assertIn("count(*)", query)
        self.assertNotIn("e.*", query)
        self.assertNotIn("body", query)
        self.assertNotIn("vectors.vector", query)

    def test_persisted_count_does_not_claim_integrity_validation(self) -> None:
        entry = self.store.remember(self.project, "Cached", "Original body.")
        self.index()
        self.store._connection.execute("UPDATE entries SET body = 'changed outside Store' WHERE id = ?", (entry["id"],))
        self.assertEqual(0, self.count())
        self.assertEqual(1, self.store.embedding_status(self.project, "test-model", "r1", 2)["stale"])

    def test_profile_project_and_closed_store_validation(self) -> None:
        for args in [("relative", "test-model", "r1", 2), (self.project, "", "r1", 2),
                     (self.project, "test-model", "", 2), (self.project, "test-model", "r1", True),
                     (self.project, "test-model", "r1", 0)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.store.pending_embedding_count(*args)
        self.store.close()
        with self.assertRaises(StoreError):
            self.count()


class SemanticPendingCountTests(unittest.TestCase):
    def test_production_path_uses_fast_count_only(self) -> None:
        store = Mock(spec=Store)
        store.pending_embedding_count.return_value = 7
        self.assertEqual(7, semantic._pending_count(store, "/project"))
        store.pending_embedding_count.assert_called_once_with(
            "/project", semantic.MODEL, semantic.MODEL_REVISION, semantic.DIMENSIONS
        )
        store.embedding_status.assert_not_called()

    def test_fast_count_invalid_results_fail_without_falling_back(self) -> None:
        for result in [True, -1, None, 1.5, "2"]:
            store = Mock(spec=Store)
            store.pending_embedding_count.return_value = result
            with self.subTest(result=result), self.assertRaises(semantic.SemanticError):
                semantic._pending_count(store, "/project")
            store.embedding_status.assert_not_called()

    def test_present_invalid_or_failing_fast_method_never_falls_back(self) -> None:
        store = Mock(spec=Store)
        store.pending_embedding_count = None
        with self.assertRaises(semantic.SemanticError):
            semantic._pending_count(store, "/project")
        store.embedding_status.assert_not_called()
        store.pending_embedding_count = Mock(side_effect=StoreError("storage failure"))
        with self.assertRaises(StoreError):
            semantic._pending_count(store, "/project")
        store.embedding_status.assert_not_called()

    def test_older_adapter_can_use_status(self) -> None:
        class OlderAdapter:
            def embedding_status(self, *args: object) -> dict[str, int]:
                return {"pending": 3}
        self.assertEqual(3, semantic._pending_count(OlderAdapter(), "/project"))


if __name__ == "__main__":
    unittest.main()
