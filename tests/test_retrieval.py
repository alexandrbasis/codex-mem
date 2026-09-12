"""Retrieval projection preserves evidence while avoiding full-text previews."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from codex_mem.retrieval import preview_records
from codex_mem.store import Store


class RetrievalPreviewTests(unittest.TestCase):
    def test_compact_preview_omits_duplicate_details_and_full_get_preserves_them(self):
        with tempfile.TemporaryDirectory() as temporary, Store(Path(temporary) / "data") as store:
            project = Path(temporary) / "project"
            narrative = ("A transaction rolls back both the source links and derived note. " * 80).strip()
            entry = store.remember(
                project, "Atomic memory write", "Transaction rollback verified in a fixture.",
                observation={"type": "bugfix", "facts": ["Source links roll back atomically."], "narrative": narrative},
            )
            detailed = store.search(project, "transaction")
            original = copy.deepcopy(detailed)
            compact = preview_records(detailed)
            self.assertEqual(detailed, original)
            self.assertEqual([entry["id"]], [record["id"] for record in compact])
            self.assertEqual({"type": "bugfix"}, compact[0]["observation"])
            self.assertEqual(detailed[0]["provenance"], compact[0]["provenance"])
            self.assertEqual(detailed[0]["score"], compact[0]["score"])
            self.assertNotIn("metadata", compact[0])
            self.assertNotIn(narrative, json.dumps(compact))
            self.assertLess(len(json.dumps(compact)), len(json.dumps(detailed)) // 4)
            self.assertEqual(detailed, preview_records(detailed, detail="full"))
            self.assertEqual(narrative, store.get(project, [entry["id"]])[0]["observation"]["narrative"])

    def test_compact_summary_keeps_historical_and_anchor_markers(self):
        record = {
            "id": "a" * 32, "title": "Handoff", "kind": "session_summary",
            "preview": "Deployment remains unverified.", "session_summary": {"notes": "Full notes"},
            "metadata": {"session_summary": {"notes": "Full notes"}},
            "is_anchor": True, "superseded_by": "b" * 32,
            "source_ids": ["c" * 32, "d" * 32],
            "provenance": {"verification": "not_assessed", "record_role": "derived_note"},
        }
        compact = preview_records([record])[0]
        self.assertTrue(compact["is_anchor"])
        self.assertEqual(record["superseded_by"], compact["superseded_by"])
        self.assertEqual(2, compact["source_count"])
        self.assertTrue(compact["details_available"])
        self.assertNotIn("session_summary", compact)
        self.assertEqual(record["preview"], compact["preview"])

    def test_invalid_detail_is_rejected_even_for_empty_results(self):
        for detail in (None, [], "brief", True):
            with self.subTest(detail=detail), self.assertRaises(ValueError):
                preview_records([], detail=detail)
