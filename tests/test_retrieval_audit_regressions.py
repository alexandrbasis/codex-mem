"""Regressions from the Sep 22 live retrieval audit, with fictional records."""
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from codex_mem import semantic
from codex_mem.config import configure
from codex_mem.retrieval import preview_records
from codex_mem.store import Store
from tests.test_resume_current_state import Backend, CandidateStore, event, summary


AUDIT_QUERIES = (
    "Как сейчас работает codex-mem и что осталось проверить?",
    "Что вошло в последний релиз и чем подтверждено, что его выкатили?",
    "Что у нас осталось непроверенным после последнего релиза?",
    "проанализируй как там наш плагин как справляется с задачей своей",
)


class AuditRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve() / "codex-mem-public"

    def search(self, query, records, *, limit=2):
        metadata = {row["id"]: event(row.pop("fixture_time")) for row in records}
        result = semantic.search(CandidateStore(records, metadata), self.project, query,
                                 mode="semantic", intent="resume", limit=limit, backend=Backend())
        return result["results"]

    def releases(self):
        return [
            summary("old", "old-session", title="Codex Mem release blocked",
                    body="The release had unresolved freshness work.",
                    fixture_time="2026-09-14T10:00:00Z"),
            summary("current", "release-session", title="Release verification completed",
                    body="The codex-mem release was pushed; native integration remains unverified.",
                    fixture_time="2026-09-21T10:00:00Z"),
        ]

    def test_natural_audit_questions_choose_latest_matching_release(self):
        for query in AUDIT_QUERIES:
            with self.subTest(query=query):
                records = self.search(query, self.releases(), limit=1)
                self.assertEqual(["current"], [r["id"] for r in records])

    def test_latest_release_keeps_release_scope_over_newer_unrelated_work(self):
        for query in (AUDIT_QUERIES[1], AUDIT_QUERIES[2],
                      "What was shipped in the latest release and what is still unverified?",
                      "What shipped in the last release?", "Как прошёл последний релиз?"):
            with self.subTest(query=query):
                records = self.releases() + [summary(
                    "newer-unrelated", "other-session", title="Invoice parser review",
                    body="Reviewed VAT rounding in the invoice parser.",
                    fixture_time="2026-09-22T10:00:00Z")]
                self.assertEqual("current", self.search(query, records, limit=1)[0]["id"])

    def test_specific_constraints_and_history_do_not_become_global_state(self):
        for query in (
            "What is the current status of raw event filtering?",
            "Что осталось проверить в фильтрации сырых событий?",
            "Что вошло в последний релиз queue-worker?",
            "What was shipped in the latest release 1.8.0?",
            "What remains unverified in AIAN-700?",
            "What is the current status of codex_mem/retrieval.py?",
            "What is the current status of memory_search?",
            "Покажи историю последних релизов codex-mem",
        ):
            with self.subTest(query=query):
                self.assertEqual("old", self.search(query, self.releases(), limit=1)[0]["id"])

    def test_same_topic_later_handoff_moves_before_old_status_note(self):
        old = {"id": "pending", "kind": "note", "session_id": "release-session",
               "source": "processor:test", "title": "Jev release push not verified",
               "body": "Jev release push was requested; completion was not verified.",
               "observation": {"type": "discovery"},
               "later_summary_id": "current", "fixture_time": "2026-09-21T09:00:00Z"}
        current = summary("current", "release-session", title="Jev release push completed",
                          body="Jev release push completed with a recorded CI result.",
                          source_ids=["pending"],
                          session_summary={"completed": "Jev release push completed and CI passed."},
                          fixture_time="2026-09-21T10:00:00Z")
        independent = {"id": "native-gap", "kind": "note", "session_id": "other-session",
                       "source": "processor:test", "title": "Native integration remains unverified",
                       "body": "Run the native probe before claiming integration coverage.",
                       "observation": {"type": "discovery"},
                       "fixture_time": "2026-09-20T10:00:00Z"}
        records = self.search("What is the current project state?", [old, current, independent], limit=2)
        self.assertEqual(["current", "native-gap"], [r["id"] for r in records])

    def test_uncited_open_status_is_marked_chronologically_without_losing_its_slot(self):
        old = {"id": "native-gap", "kind": "note", "session_id": "release-session",
               "source": "processor:test", "title": "Native integration remains unverified",
               "body": "Run the native probe before claiming integration coverage.",
               "created_at": "2026-09-21T09:00:00Z", "observation": {"type": "discovery"},
               "later_summary_id": "handoff", "fixture_time": "2026-09-21T09:00:00Z"}
        handoff = summary("handoff", "release-session", title="Release checks completed",
            source_ids=["different-receipt"], session_summary={"completed": "Release CI passed.",
                "next_steps": "Native integration remains unverified; run the native probe."},
            fixture_time="2026-09-21T10:00:00Z")
        newer = summary("newer", "other-session", title="Current project review",
                        fixture_time="2026-09-22T10:00:00Z")
        outside = {"id": "outside", "kind": "note", "title": "Independent finding",
                   "observation": {"type": "discovery"}, "fixture_time": "2026-09-20T10:00:00Z"}
        records = self.search("What is the current project state?", [old, handoff, newer, outside], limit=3)
        self.assertEqual(["newer", "handoff", "native-gap"], [r["id"] for r in records])
        status = records[2]
        self.assertTrue(status.get("earlier_status"))
        self.assertFalse(status.get("context_before_handoff"))
        self.assertFalse(status.get("superseded_by"))
        self.assertIn("Run the native probe", status["body"])
        self.assertTrue(preview_records([status])[0].get("earlier_status"))
        markup = ET.fromstring(Store._context_entry_markup(status, 2000))
        self.assertEqual("earlier_status", markup.attrib.get("selection"))
        self.assertEqual("handoff", markup.attrib.get("later_summary_id"))
        self.assertIn("not proof", markup.findtext("notice"))
        self.assertIn("resolved", markup.findtext("notice"))

    def test_earlier_status_requires_selected_same_session_later_handoff(self):
        for later_session, later_time, limit in (
            ("other-session", "2026-09-21T10:00:00Z", 3),
            ("release-session", "2026-09-21T08:00:00Z", 3),
            ("release-session", "2026-09-21T10:00:00Z", 2),
        ):
            with self.subTest(session=later_session, at=later_time, limit=limit):
                old = {"id": "pending", "kind": "note", "session_id": "release-session",
                       "title": "Native integration remains unverified", "observation": {"type": "discovery"},
                       "later_summary_id": "handoff", "fixture_time": "2026-09-21T09:00:00Z"}
                handoff = summary("handoff", later_session, title="Release handoff",
                                  fixture_time=later_time)
                newer = summary("newer", "new-session", title="Project review",
                                fixture_time="2026-09-22T10:00:00Z")
                records = self.search("What is the current project state?", [old, handoff, newer], limit=limit)
                status = next(row for row in records if row["id"] == "pending")
                self.assertFalse(status.get("earlier_status"))

    def test_later_handoff_does_not_hide_negated_or_still_open_outcomes(self):
        for completed, next_steps in (
            ("Jev release push was not completed.", ""),
            ("Jev release push не выполнено.", ""),
            ("Jev release push checklist completed.", "Jev release push remains pending."),
            ("Read Jev release push documentation.", ""),
        ):
            with self.subTest(completed=completed):
                old = {"id": "pending", "kind": "note", "session_id": "release-session",
                       "title": "Jev release push not verified", "observation": {"type": "discovery"},
                       "later_summary_id": "current", "fixture_time": "2026-09-21T09:00:00Z"}
                current = summary("current", "release-session", title="Jev release push followup",
                    source_ids=["pending"], session_summary={"completed": completed, "next_steps": next_steps},
                    fixture_time="2026-09-21T10:00:00Z")
                other = {"id": "other", "kind": "note", "title": "Independent topic",
                         "observation": {"type": "discovery"}, "fixture_time": "2026-09-20T10:00:00Z"}
                self.assertIn("pending", [r["id"] for r in self.search(
                    "What is the current project state?", [old, current, other], limit=2)])

    def test_handoff_chronology_compares_instants_with_timezones(self):
        old = {"id": "pending", "kind": "note", "session_id": "release-session",
               "title": "Jev release push not verified", "observation": {"type": "discovery"},
               "later_summary_id": "current", "fixture_time": "2026-09-21T12:00:00+03:00"}
        current = summary("current", "release-session", title="Jev release push completed",
            source_ids=["pending"], session_summary={"completed": "Jev release push completed."},
            fixture_time="2026-09-21T10:00:00Z")
        other = {"id": "other", "kind": "note", "title": "Independent topic",
                 "observation": {"type": "discovery"}, "fixture_time": "2026-09-20T10:00:00Z"}
        self.assertEqual(["current", "other"], [r["id"] for r in self.search(
            "What is the current project state?", [old, current, other], limit=2)])

    def test_explicit_later_handoff_outside_candidate_window_is_read_boundedly(self):
        old = summary("old", "same-session", title="Jev filtering plan pending")
        later = summary("later", "same-session", project=str(self.project),
                        title="Jev filtering implementation checked", source="processor:test",
                        body="Implementation completed; native integration remains unverified.",
                        session_summary={"completed": "Jev filtering implementation checked.",
                                         "next_steps": "Native integration remains unverified."})
        class LinkedStore(CandidateStore):
            def get(self, project, ids):
                self.get_ids = ids
                return [dict(later)]
        store = LinkedStore([old], {"old": event("2026-09-19T10:00:00Z", later_summary_id="later"),
                                    "later": event("2026-09-19T11:00:00Z")})
        result = semantic.search(store, self.project, "What is the current project state?",
                                 mode="semantic", intent="resume", limit=1, backend=Backend())
        self.assertEqual(["later"], [row["id"] for row in result["results"]])
        self.assertEqual(["later"], store.get_ids)
        self.assertIn("Native integration remains unverified.", result["results"][0]["session_summary"]["next_steps"])
        self.assertNotIn("body", result["results"][0])
        self.assertEqual(1, result["resume_linked_handoffs"])

    def test_explicit_filters_and_specific_queries_disable_handoff_hydration(self):
        class LinkedStore(CandidateStore):
            def get(self, project, ids):
                raise AssertionError("Filtered or topical searches must not widen to linked records")
        for filters in ({"kinds": ["session_summary"]}, {"types": ["discovery"]},
                        {"files": ["release.py"]}, {"concepts": ["release"]}):
            with self.subTest(filters=filters):
                store = LinkedStore([summary("old", "same-session")],
                    {"old": event("2026-09-19T10:00:00Z", later_summary_id="later")})
                result = semantic.search(store, self.project, "What is the current project state?",
                    mode="semantic", intent="resume", limit=1, backend=Backend(), **filters)
                self.assertNotIn("later", [row["id"] for row in result["results"]])
        for query in ("What remains for Jev filtering?", "Show project state history"):
            store = LinkedStore([summary("old", "same-session")],
                {"old": event("2026-09-19T10:00:00Z", later_summary_id="later")})
            semantic.search(store, self.project, query, mode="semantic", intent="resume", backend=Backend())


class AuditContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "codex-mem-public"
        data = Path(self.temp.name) / "data"
        configure(data, semantic_enabled=False, processor_enabled=False, service_enabled=False)
        self.store = Store(data)
        self.addCleanup(self.store.close)

    def note(self, title, body, session, day, **kwargs):
        raw = self.store.remember(self.project, "Source", "Fixture receipt.",
                                  source="hook:Stop", session_id=session)
        self.store._connection.execute("UPDATE entries SET created_at=? WHERE id=?",
            (f"2026-09-{day:02d}T10:00:00Z", raw["id"]))
        return self.store.remember(self.project, title, body, source="processor:test",
            session_id=session, source_ids=[raw["id"]], kind="session_summary", **kwargs)

    def ids(self, query):
        context = self.store.context(self.project, query=query, budget=6000)
        return [row.attrib["id"] for row in ET.fromstring(context).findall("entry")]

    def test_automatic_context_uses_current_evidence_across_language(self):
        self.note("Плагин Codex Mem: старый релиз", "Плагин памяти задерживает обработку.",
                  "old", 14, session_summary={"next_steps": "Проверить обработку памяти."})
        latest = self.note("Release verification completed", "The codex-mem release was pushed.",
                          "current", 21, session_summary={"completed": "CI passed.",
                          "next_steps": "Native integration remains unverified."})
        for query in AUDIT_QUERIES:
            with self.subTest(query=query):
                self.assertEqual(latest["id"], self.ids(query)[0])

    def test_release_context_excludes_newer_unrelated_records(self):
        release = self.note("Release verification completed", "The release was pushed.", "release", 21)
        self.note("Invoice parser review", "Reviewed invoice rounding.", "newer", 22)
        self.assertEqual([release["id"]], self.ids(AUDIT_QUERIES[1]))

    def test_direct_skill_mention_normalizes_without_relaxing_identifiers(self):
        note = self.store.remember(self.project, "memory_search 1.9.0", "Lookup behavior.")
        self.store.remember(self.project, "memory_search 1.8.0", "Old behavior.")
        mention = " [$codex-mem:maintenance](/tmp/plugin/skills/maintenance/SKILL.md)"
        self.assertEqual([note["id"]], self.ids("How does memory_search work in 1.9.0?" + mention))
        self.assertEqual([], self.ids(mention))


if __name__ == "__main__":
    unittest.main()
