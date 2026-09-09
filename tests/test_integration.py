"""Queue boundaries distinguish absent optional setup from broken artifacts."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_mem.integration import index_project
from codex_mem.integration import search_memory
from codex_mem.store import Store
from codex_mem.service import _index_pending


class IntegrationTests(unittest.TestCase):
    def test_search_memory_propagates_structured_filters_to_semantic_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "memory"
            project = Path(temporary) / "project"
            project.mkdir()
            with Store(data) as store:
                with patch(
                    "codex_mem.semantic.search",
                    return_value={
                        "results": [],
                        "requested_mode": "lexical",
                        "used_mode": "lexical",
                        "fallback_reason": None,
                    },
                ) as search:
                    result = search_memory(
                        store,
                        project,
                        "query",
                        mode="lexical",
                        limit=3,
                        kinds=["note"],
                        types=["bugfix"],
                        concepts=["sqlite"],
                        files=["codex_mem/store.py"],
                    )
            self.assertEqual([], result["results"])
            self.assertEqual(
                {
                    "mode": "lexical",
                    "limit": 3,
                    "kinds": ["note"],
                    "types": ["bugfix"],
                    "concepts": ["sqlite"],
                    "files": ["codex_mem/store.py"],
                },
                {
                    "mode": search.call_args.kwargs["mode"],
                    "limit": search.call_args.kwargs["limit"],
                    "kinds": search.call_args.kwargs["kinds"],
                    "types": search.call_args.kwargs["types"],
                    "concepts": search.call_args.kwargs["concepts"],
                    "files": search.call_args.kwargs["files"],
                },
            )

    def test_roadmap_resume_promotes_handoff_without_hiding_narrow_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "memory"
            project = Path(temporary) / "project"
            project.mkdir()
            with Store(data) as store:
                detail = store.remember(project, "Roadmap roadmap theme", "Roadmap CSS token midnightblue.")
                summary = store.remember(
                    project, "Release handoff", "The roadmap release uses autosave; marketplace verification remains open.",
                    kind="session_summary",
                )
                decision = store.remember(
                    project, "Editing decision", "Use autosave for roadmap changes because drafts caused lost edits.",
                    observation={"type": "decision"},
                )
                unrelated = store.remember(project, "Other release", "Billing export completed.", kind="session_summary")
                lookup = search_memory(store, project, "roadmap", mode="lexical", limit=2)
                self.assertEqual("lookup", lookup["intent"])
                self.assertEqual(detail["id"], lookup["results"][0]["id"])
                resumed = search_memory(store, project, "roadmap", mode="auto", intent="resume", limit=2)
                self.assertEqual("resume", resumed["intent"])
                self.assertEqual([summary["id"], decision["id"]], [item["id"] for item in resumed["results"]])
                self.assertNotIn(unrelated["id"], [item["id"] for item in resumed["results"]])
                narrow = search_memory(store, project, "midnightblue", mode="lexical", limit=1)
                self.assertEqual([detail["id"]], [item["id"] for item in narrow["results"]])
                # Ranking must not mutate source history or invent supersession.
                self.assertEqual(4, len(store.timeline(project)))
                self.assertIsNone(store.get(project, [detail["id"]])[0]["superseded_by"])

    def test_absent_optional_model_does_not_block_observation_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "memory"
            with patch("codex_mem.semantic.index_pending", return_value={
                "status": "unavailable", "code": "model_not_ready", "indexed": 0, "pending": 0,
            }):
                result = index_project(Path(temporary), data)
            self.assertEqual("unavailable", result["status"])
            self.assertFalse(_index_pending(result))

    def test_bad_model_hash_blocks_queue_until_explicit_repair(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "memory"
            with patch("codex_mem.semantic.index_pending", return_value={
                "status": "unavailable", "code": "model_hash_mismatch", "indexed": 0, "pending": 0,
            }):
                result = index_project(Path(temporary), data)
            self.assertEqual("failed", result["status"])
            from codex_mem.service import ServiceError
            with self.assertRaises(ServiceError):
                _index_pending(result)
