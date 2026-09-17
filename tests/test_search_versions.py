"""Literal dotted-version retrieval must not confuse unrelated numeric metadata."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem.store import Store


class VersionSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.store = Store(self.root / "memory-home")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def ids(self, query: str, **filters: object) -> set[str]:
        return {row["id"] for row in self.store.search(self.project, query, **filters)}

    def test_version_matches_title_body_and_optional_v_prefix(self) -> None:
        entries = [
            self.store.remember(self.project, "Release v1.9.0", "Published."),
            self.store.remember(self.project, "Installed package", "Now running 1.9.0."),
            self.store.remember(self.project, "Release V1.9.0", "Verified."),
        ]
        for query in ("1.9.0", "v1.9.0", "V1.9.0", "１．９．０"):
            with self.subTest(query=query):
                self.assertEqual({entry["id"] for entry in entries}, self.ids(query))

    def test_version_does_not_match_separate_numbers_or_larger_versions(self) -> None:
        expected = self.store.remember(self.project, "Release 1.9.0", "Published.")
        for version in (
            "1.9.01", "11.9.0", "1.9.0.1", "v1.9.0.1", "0.1.9.0", "x1.9.0", "1 9 0", "v1 9 0",
        ):
            self.store.remember(self.project, f"Other release {version}", "Published.")
        self.assertEqual({expected["id"]}, self.ids("1.9.0"))

    def test_version_does_not_match_ids_or_file_artifact_numbers(self) -> None:
        with mock.patch("codex_mem.store.uuid.uuid4") as identifier:
            identifier.return_value.hex = "190" * 10 + "19"
            source = self.store.remember(self.project, "Evidence", "Tool output.")
        self.store.remember(
            self.project,
            "Unrelated summary",
            "A connection check completed.",
            session_summary={"completed": "A connection check completed.", "source_ids": [source["id"]]},
        )
        for path in ("/tmp/audit/1/9/0.json", "/tmp/audit/1.9.0.json"):
            self.store.remember(
                self.project,
                "Connection check",
                "The service is reachable.",
                observation={"type": "discovery", "files_read": [path]},
            )
        self.assertEqual(set(), self.ids("1.9.0"))

    def test_version_matches_meaningful_metadata(self) -> None:
        entries = [
            self.store.remember(
                self.project, "Package audit", "Done.",
                observation={"type": "discovery", "facts": ["Installed v1.9.0."]},
            ),
            self.store.remember(
                self.project, "Package audit", "Done.",
                observation={"type": "discovery", "narrative": "Installed 1.9.0."},
            ),
            self.store.remember(
                self.project, "Package handoff", "Done.",
                session_summary={"completed": "Installed 1.9.0."},
            ),
        ]
        self.store.remember(
            self.project, "Unrelated audit", "Done.",
            observation={"type": "discovery", "facts": ["1 successful run, 9 tests, 0 failures."]},
        )
        self.store.remember(
            self.project, "Unrelated release", "Done.",
            session_summary={"completed": "Installed 1.9.01 and 11.9.0."},
        )
        self.assertEqual({entry["id"] for entry in entries}, self.ids("1.9.0"))

    def test_version_and_ordinary_words_both_remain_required(self) -> None:
        title_match = self.store.remember(self.project, "Release v1.9.0", "Verified.")
        metadata_match = self.store.remember(
            self.project, "Package audit", "Done.",
            observation={"type": "discovery", "facts": ["Release 1.9.0 verified."]},
        )
        self.store.remember(self.project, "Installation 1.9.0", "Verified.")
        self.store.remember(self.project, "Release 1.8.0", "Verified.")
        self.assertEqual({title_match["id"], metadata_match["id"]}, self.ids("release 1.9.0"))

    def test_exact_filter_runs_before_the_result_limit(self) -> None:
        with mock.patch("codex_mem.store._utc_now", return_value="2026-01-01T00:00:00.000000Z"):
            expected = self.store.remember(self.project, "Release 1.9.0", "Published.")
        for index in range(15):
            self.store.remember(self.project, "Release 1 9 0", f"Unrelated check {index}.")
        self.assertEqual({expected["id"]}, self.ids("1.9.0", limit=1))

    def test_context_uses_the_same_exact_version_boundary(self) -> None:
        expected = self.store.remember(self.project, "Release v1.9.0", "Published.")
        unrelated = self.store.remember(self.project, "Release 1 9 0", "Unrelated.")
        metadata_only = self.store.remember(
            self.project, "Package audit", "Done.",
            observation={"type": "discovery", "facts": ["Installed 1.9.0."]},
        )
        context = self.store.context(self.project, query="1.9.0", budget=10_000)
        self.assertIn(expected["id"], context)
        self.assertIn(metadata_only["id"], context)
        self.assertNotIn(unrelated["id"], context)

    def test_ordinary_unicode_search_and_metadata_filters_are_preserved(self) -> None:
        expected = self.store.remember(
            self.project, "Настройка café v1.9.0", "Verified.",
            observation={"type": "bugfix", "concepts": ["deployment"], "files_modified": ["src/café.py"]},
        )
        self.store.remember(self.root / "other-project", "Настройка café v1.9.0", "Other project.")
        for query in ("настройка", "café", "настройка 1.9.0"):
            with self.subTest(query=query):
                self.assertEqual(
                    {expected["id"]},
                    self.ids(query, types="bugfix", concepts="deployment", files="src/café.py"),
                )
        self.assertEqual([], self.store.search(self.project, "1.9.0", types="discovery"))

    def test_query_punctuation_cannot_create_fts_or_sql_operators(self) -> None:
        self.store.remember(self.project, "Release 1.9.0", "Published.")
        for query in ('1.9.0" OR *', "1.9.0') OR 1=1--", "1.9.0.*"):
            with self.subTest(query=query):
                rows = self.store.search(self.project, query)
                self.assertIsInstance(rows, list)
        self.assertEqual([], self.store.search(self.project, '1.9.0" OR *'))

    def test_unrelated_malformed_metadata_is_not_parsed_as_version_prose(self) -> None:
        entry = self.store.remember(
            self.project, "Connection audit", "Done.",
            observation={"type": "discovery", "facts": ["The service is reachable."]},
        )
        self.store._connection.execute(
            "UPDATE entry_metadata SET session_summary_json = ? WHERE entry_id = ?",
            ("{", entry["id"]),
        )
        self.assertEqual(set(), self.ids("1.9.0"))
        self.assertNotIn(entry["id"], self.store.context(self.project, query="1.9.0", budget=1_000))


if __name__ == "__main__":
    unittest.main()
