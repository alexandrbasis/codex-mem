"""Cross-session followups identify later evidence without erasing history."""
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from codex_mem import semantic
from codex_mem.config import configure
from codex_mem.retrieval import preview_records
from codex_mem.store import Store


class TopicFollowupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.data = Path(self.temp.name) / "data"
        configure(self.data, semantic_enabled=False, service_enabled=False, processor_enabled=False)
        self.store = Store(self.data)
        self.addCleanup(self.store.close)

    def note(self, title, session, day, *, next_steps="", completed="", tool=True,
             body=None, project=None, version="", recorded_day=None):
        project = project or self.project
        sources = []
        for source in (["hook:PostToolUse", "hook:Stop"] if tool else ["hook:Stop"]):
            raw = self.store.remember(project, "Source receipt", "Private source text.",
                                      source=source, session_id=session)
            self.store._connection.execute("UPDATE entries SET created_at=? WHERE id=?",
                (f"2026-09-{day:02d}T10:00:00Z", raw["id"]))
            sources.append(raw["id"])
        record = self.store.remember(project, title + version, body or title,
            kind="session_summary", session_id=session, source="processor:test", source_ids=sources,
            session_summary={"request": title, "next_steps": next_steps, "completed": completed})
        self.store._connection.execute("UPDATE entries SET created_at=? WHERE id=?",
            (f"2026-09-{recorded_day or day:02d}T10:01:00Z", record["id"]))
        return record

    def test_later_evidence_links_old_plan_across_sessions_and_keeps_history(self):
        old = self.note("Checkout retry key fix pending", "planning", 10,
                        next_steps="Implement checkout retry key fix and verify the regression.")
        new = self.note("Checkout retry key fix checked", "implementation", 11,
                        completed="Checkout retry key regression passed in 23 local tests.")
        metadata = self.store.resume_metadata(self.project, [old["id"], new["id"]])
        self.assertEqual(new["id"], metadata[old["id"]]["later_context_id"])
        self.assertEqual("related_later_evidence", metadata[old["id"]]["later_context_relation"])
        self.assertNotIn("context_historical", metadata[old["id"]])
        self.assertNotIn("later_context_id", metadata[new["id"]])
        stored = self.store.get(self.project, [old["id"]])[0]
        self.assertIsNone(stored["superseded_by"])
        self.assertIn("Implement", stored["session_summary"]["next_steps"])
        lookup = self.store.search(self.project, "checkout")
        self.assertEqual({old["id"], new["id"]}, {r["id"] for r in lookup})
        preview = preview_records(lookup)
        earlier = next(r for r in preview if r["id"] == old["id"])
        self.assertEqual(new["id"], earlier["later_context_id"])

    def test_resume_and_context_keep_later_result_and_unrelated_work(self):
        old = self.note("Checkout retry key fix pending", "planning", 10,
                        next_steps="Implement checkout retry key fix and verify the regression.")
        new = self.note("Checkout retry key fix checked", "implementation", 11,
                        completed="Checkout retry key regression passed in 23 local tests.")
        other = self.note("Checkout invoice parser pending", "invoices", 9,
                          next_steps="Investigate invoice parser VAT rounding.")
        result = semantic.search(self.store, self.project, "checkout", mode="lexical", intent="resume")
        resumed_ids = [r["id"] for r in result["results"]]
        self.assertEqual({new["id"], other["id"], old["id"]}, set(resumed_ids))
        self.assertLess(resumed_ids.index(new["id"]), resumed_ids.index(old["id"]))
        context = self.store.context(self.project, query="checkout", budget=6000)
        ids = [r.attrib["id"] for r in ET.fromstring(context).findall("entry")]
        self.assertEqual([new["id"], other["id"], old["id"]], ids)
        self.assertIn("Implement checkout retry key fix", context)
        self.assertIsNone(self.store.get(self.project, [old["id"]])[0]["superseded_by"])

    def test_newer_timestamp_assistant_report_or_other_version_is_not_followup(self):
        old = self.note("Checkout retry key fix pending", "planning", 10, version=" 1.8.0")
        self.note("Checkout retry key fix done", "assistant-only", 11, tool=False,
                  completed="Reported completion.", version=" 1.8.0")
        self.note("Checkout retry key fix checked", "other-version", 12,
                  completed="Local tests passed.", version=" 1.9.0")
        self.note("Checkout invoice parser checked", "other-topic", 13,
                  completed="Invoice checks passed.", version=" 1.8.0")
        self.assertNotIn("later_context_id", self.store.resume_metadata(self.project, [old["id"]])[old["id"]])

    def test_delayed_old_processing_does_not_reverse_source_chronology(self):
        old = self.note("Checkout retry key fix pending", "planning", 10, recorded_day=15,
                        next_steps="Verify checkout retry key regression.")
        new = self.note("Checkout retry key fix checked", "implementation", 11,
                        completed="Checkout retry key regression passed.")
        rows = self.store.resume_metadata(self.project, [old["id"], new["id"]])
        self.assertEqual(new["id"], rows[old["id"]]["later_context_id"])
        self.assertNotIn("later_context_id", rows[new["id"]])

    def test_filtered_match_keeps_old_record_with_uncertain_followup_pointer(self):
        old = self.note("Checkout retry key proposal pending", "planning", 10,
                        next_steps="Proposal: implement checkout retry key fix.")
        new = self.note("Checkout retry key regression still fails", "implementation", 11,
                        completed="Checkout retry key regression was run; it failed.")
        context = self.store.context(self.project, query="proposal", budget=6000)
        entries = ET.fromstring(context).findall("entry")
        self.assertEqual([old["id"]], [r.attrib["id"] for r in entries])
        self.assertEqual(new["id"], entries[0].attrib["later_context_id"])
        self.assertIn("not proof", entries[0].findtext("notice"))
        self.assertNotIn("current session state", context)
        self.assertNotIn("Private source text", context)

    def test_cross_project_and_current_session_followups_do_not_leak_into_context(self):
        old = self.note("Checkout retry key fix pending", "planning", 10)
        self.note("Checkout retry key fix checked", "foreign", 11, project=self.project / "foreign",
                  completed="FOREIGN_PRIVATE")
        current = self.note("Checkout retry key fix checked", "current", 12, completed="CURRENT_PRIVATE")
        context = self.store.context(self.project, query="checkout", exclude_session="current", budget=6000)
        self.assertIn(old["id"], context)
        self.assertNotIn(current["id"], context)
        self.assertNotIn("PRIVATE", context)

    def test_later_document_read_does_not_remove_still_open_requirement(self):
        old = self.note("Checkout retry key release pending", "planning", 10,
                        next_steps="Deploy checkout retry key to production and verify live traffic.")
        later = self.note("Checkout retry key documentation read", "diagnostic", 11,
                          completed="Read checkout retry key documentation; deployment remains unverified.")
        result = semantic.search(self.store, self.project, "checkout", mode="lexical", intent="resume")
        self.assertEqual({old["id"], later["id"]}, {r["id"] for r in result["results"]})
        earlier = next(r for r in result["results"] if r["id"] == old["id"])
        self.assertIn("Deploy", earlier["session_summary"]["next_steps"])
        self.assertNotIn("superseded", earlier.get("later_context_relation", ""))
        context = self.store.context(self.project, query="checkout", budget=6000)
        self.assertIn("Deploy checkout retry key to production", context)
        self.assertIn("not proof", context)

    def test_parallel_subtasks_do_not_get_a_false_topic_link(self):
        old = self.note("Checkout retry metrics dashboard pending", "dashboard", 10,
                        next_steps="Build checkout retry metrics dashboard.")
        later = self.note("Checkout retry key deduplication checked", "deduplication", 11,
                          completed="Checkout retry key deduplication passed local checks.")
        metadata = self.store.resume_metadata(self.project, [old["id"], later["id"]])
        self.assertNotIn("later_context_id", metadata[old["id"]])
        result = semantic.search(self.store, self.project, "checkout", mode="lexical", intent="resume")
        self.assertEqual({old["id"], later["id"]}, {r["id"] for r in result["results"]})

    def test_different_exact_issue_or_api_scope_does_not_get_a_topic_link(self):
        for old_scope, new_scope in (("AIAN-700", "AIAN-701"), ("memory_search", "memory_get"),
                                     ("AIAN-800 memory_search", "AIAN-801 memory_search")):
            with self.subTest(old=old_scope, new=new_scope):
                old = self.note(f"{old_scope} checkout retry fix pending", "old-" + old_scope, 10,
                                next_steps=f"Implement {old_scope} checkout retry fix.")
                self.note(f"{new_scope} checkout retry fix checked", "new-" + new_scope, 11,
                          completed=f"{new_scope} checkout retry fix passed local checks.")
                metadata = self.store.resume_metadata(self.project, [old["id"]])
                self.assertNotIn("later_context_id", metadata[old["id"]])

    def test_related_read_cannot_push_old_obligation_out_of_context_or_resume_limit(self):
        old = self.note("Checkout retry release pending", "old", 10,
                        next_steps="Deploy checkout retry to production and verify live traffic.")
        read = self.note("Checkout retry documentation read", "later", 11,
                         completed="Checkout retry documentation was read; deployment remains unverified.")
        self.note("Checkout tax parser pending", "other-summary", 9,
                  next_steps="Review checkout tax parser.")
        for topic in ("fraud detection", "VAT rounding"):
            self.store.remember(self.project, "Checkout " + topic, "Independent open work.",
                                kind="decision", session_id=topic)
        result = semantic.search(self.store, self.project, "checkout", mode="lexical", intent="resume", limit=4)
        self.assertIn(old["id"], [record["id"] for record in result["results"]])
        context = self.store.context(self.project, query="checkout", budget=6000)
        self.assertIn(old["id"], context)
        self.assertIn(read["id"], context)
        self.assertIn("Deploy checkout retry to production", context)
