"""Project memory at the existing Store API and context boundaries."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from codex_mem.config import configure
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store


class ProjectMemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = str(Path(temporary.name) / "project")
        self.other = str(Path(temporary.name) / "other")
        self.store = Store(Path(temporary.name) / "data")
        self.addCleanup(self.store.close)
        configure(self.store.data_dir, capture_scope="all", project_memory_mode="pilot",
                  project_memory_projects=[self.project])
        self.memory = ProjectMemory(self.store)

    def source(self, text, session="earlier"):
        return self.store.remember(self.project, "User requirement", text,
                                   source="hook:UserPromptSubmit", session_id=session)

    def claim(self, text="Importer deployment remains open.", session="earlier"):
        source = self.source(text, session)
        job = self.memory.claim(self.project)
        span = {"source_id": source["id"], "start": 0, "end": len(text), "quote": text}
        result = self.memory.commit(job, {
            "episodes": [],
            "claims": [{"key": "deployment", "kind": "requirement", "workflow_state": "open",
                        "text": text, "evidence": {"text": [span], "workflow_state": [span]}}],
            "relations": [],
        })
        self.assertEqual("committed", result["status"])
        return source, result["object_ids"][0]

    def test_context_and_exact_get_expose_project_scoped_evidence(self):
        source, claim_id = self.claim()
        self.store.remember(self.project, "Legacy note", "Importer schedule uses a local job.")
        context = self.store.context(self.project, query="importer", budget=6000, allow_remote=False)
        self.assertIn("<project-memory ", context)
        self.assertIn(claim_id, context)
        self.assertIn(source["id"], context)
        self.assertIn("Legacy note", context)
        self.assertLessEqual(len(context), 6000)
        ElementTree.fromstring(context)
        full = self.store.get(self.project, [claim_id, source["id"]])
        self.assertEqual([claim_id, source["id"]], [item["id"] for item in full])
        self.assertEqual(source["id"], full[0]["evidence"]["text"][0]["source_id"])
        self.assertEqual([], self.store.get(self.other, [claim_id, source["id"]]))
        self.assertNotIn(claim_id, self.store.context(self.other, allow_remote=False))

    def test_shadow_excluded_session_and_filters_preserve_original_context(self):
        _, claim_id = self.claim(session="current")
        self.assertNotIn(claim_id, self.store.context(self.project, exclude_session="current",
                                                     allow_remote=False))
        self.assertNotIn(claim_id, self.store.context(self.project, kinds=["note"], allow_remote=False))
        configure(self.store.data_dir, project_memory_mode="shadow")
        self.assertNotIn("<project-memory ", self.store.context(self.project, allow_remote=False))
        configure(self.store.data_dir, project_memory_mode="off")
        self.assertNotIn("<project-memory ", self.store.context(self.project, allow_remote=False))

    def test_event_and_source_write_are_atomic_on_capture_failure(self):
        with patch("codex_mem.project_memory.capture_event", side_effect=ValueError("capture failed")):
            with self.assertRaises(ValueError):
                self.source("An atomic event must not be partially saved.")
        self.assertEqual(0, self.store._connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
        self.assertEqual([], self.memory.pending_projects())

    def test_active_evidence_survives_age_prune_but_explicit_forget_revokes_it(self):
        source, claim_id = self.claim()
        disposable = self.store.remember(self.project, "Old note", "Discard this stale incidental note.")
        self.store._connection.execute("UPDATE entries SET created_at='2000-01-01T00:00:00.000000Z'")
        self.assertEqual(1, self.store.prune(days=90)["deleted"])
        self.assertEqual([], self.store.get(self.project, [disposable["id"]]))
        self.assertEqual(2, len(self.store.get(self.project, [source["id"], claim_id])))
        self.assertEqual(1, self.store.forget(self.project, [source["id"]])["deleted"])
        self.assertEqual([], self.store.get(self.project, [source["id"], claim_id]))
        self.assertNotIn("Importer deployment", self.store.context(self.project, allow_remote=False))

    def test_source_forget_fences_running_worker(self):
        source = self.source("Do not deploy until the approval arrives.")
        job = self.memory.claim(self.project)
        self.store.forget(self.project, [source["id"]])
        self.assertEqual("stale", self.memory.commit(job, {"episodes": [], "claims": [],
                                                          "relations": []})["status"])
        self.assertEqual([], self.memory.pending_projects())
        self.assertEqual([], self.store.get(self.project, [source["id"]]))

    def test_exact_derived_forget_uses_existing_store_api(self):
        source, claim_id = self.claim()
        result = self.store.forget(self.project, [claim_id])
        self.assertEqual({"deleted": 1, "ids": [claim_id]}, result)
        self.assertEqual([], self.store.get(self.project, [claim_id]))
        self.assertEqual(1, len(self.store.get(self.project, [source["id"]])))

    def test_source_forget_revokes_legacy_derivations_and_their_indexes(self):
        source, claim_id = self.claim(text="Deployment must preserve DELETE_SENSITIVE_SOURCE.")
        note = self.store.remember(self.project, "Observer summary", source["body"],
                                   source="processor:observer", source_ids=[source["id"]])
        summary = self.store.remember(self.project, "Later consolidated summary", source["body"],
                                      source_ids=[note["id"]])
        independent = self.store.remember(self.project, "Independent note", "Retain this other task.")
        result = self.store.forget(self.project, [source["id"]])
        self.assertEqual({note["id"], summary["id"]}, set(result["dependent_entry_ids"]))
        self.assertEqual([], self.store.get(self.project, [source["id"], claim_id,
                                                          note["id"], summary["id"]]))
        self.assertEqual([], self.store.search(self.project, "DELETE_SENSITIVE_SOURCE"))
        self.assertEqual(1, len(self.store.get(self.project, [independent["id"]])))

    def test_context_stays_valid_under_small_budgets(self):
        self.claim()
        for budget in (256, 512, 1000, 2000):
            context = self.store.context(self.project, budget=budget, allow_remote=False)
            self.assertLessEqual(len(context), budget)
            ElementTree.fromstring(context)


if __name__ == "__main__":
    unittest.main()
