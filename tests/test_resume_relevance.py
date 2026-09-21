"""Resume preserves multilingual relevance while collapsing repeated handoffs."""
from pathlib import Path
import tempfile
import unittest

from codex_mem import semantic
from codex_mem.retrieval import historical_query
from tests.test_resume_current_state import Backend, CandidateStore, event, summary


class SemanticCandidateStore(CandidateStore):
    def search(self, project, query, **kwargs):
        # English notes can match a Russian question semantically without
        # matching the strict lexical query at all.
        return []


class ResumeRelevanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"

    def search(self, records, metadata, query, limit=5):
        return semantic.search(SemanticCandidateStore(records, metadata), self.project,
            query, mode="semantic", intent="resume", limit=limit, backend=Backend())["results"]

    def test_distinct_russian_questions_keep_their_english_topic_matches(self):
        cases = (
            ("Как работает захват наблюдений и что осталось улучшить?", "Memory capture and recall audit"),
            ("Что мы решили насчёт фильтрации сырых событий перед записью памяти?",
             "Raw event eligibility filtering decision"),
            ("Что осталось незавершённым после улучшений восстановления очереди?",
             "Remaining resilience and recall verification work"),
        )
        for query, title in cases:
            with self.subTest(query=query):
                records = [
                    summary("topical", "topic-session", title=title),
                    {"id": "topical-evidence", "kind": "note", "title": title,
                     "observation": {"type": "discovery"}},
                    {"id": "generic-preference", "kind": "note",
                     "title": "Пользовательский приоритет улучшений памяти",
                     "observation": {"type": "decision"}},
                    summary("unrelated-newer", "other-session", title="Jev cache inspection"),
                ]
                metadata = {"topical": event("2026-09-17T11:00:00Z"),
                            "unrelated-newer": event("2026-09-19T11:00:00Z")}
                results = self.search(records, metadata, query, limit=2)
                self.assertEqual(["topical", "topical-evidence"], [r["id"] for r in results])

    def test_broad_current_state_questions_prefer_recent_handoffs_and_findings(self):
        records = [
            summary("old-release", "old-session", title="Memory plugin 1.7 installation",
                    created_at="2026-09-20T11:00:00Z"),
            {"id": "old-preference", "kind": "note", "observation": {"type": "decision"}},
            summary("current-work", "current-session", title="Recent verification and remaining work"),
            {"id": "recent-evidence", "kind": "note", "observation": {"type": "discovery"}},
        ]
        metadata = {"old-release": event("2026-09-12T11:00:00Z"),
                    "old-preference": event("2026-09-09T11:00:00Z"),
                    "current-work": event("2026-09-19T11:00:00Z"),
                    "recent-evidence": event("2026-09-19T10:00:00Z")}
        for query in (
            "Как работает плагин памяти и что осталось улучшить?",
            "Что осталось незавершённым после последних улучшений плагина?",
            "Каково текущее состояние проекта?",
            "How is the memory plugin doing and what remains to improve?",
            "What remains unfinished after the latest plugin improvements?",
            "What is the current state of the project?",
        ):
            with self.subTest(query=query):
                results = self.search(records, metadata, query, limit=2)
                self.assertEqual(["current-work", "recent-evidence"], [r["id"] for r in results])

    def test_current_word_never_erases_a_specific_topic_version_issue_or_history(self):
        records = [summary("relevant", "topic-session"), summary("newer", "other-session")]
        metadata = {"relevant": event("2026-09-17T11:00:00Z"),
                    "newer": event("2026-09-19T11:00:00Z")}
        for query in (
            "What is the current status of raw event filtering?",
            "Что осталось улучшить в фильтрации сырых событий перед записью памяти?",
            "What is the current state of memory_search?",
            "Что осталось незавершённым в AIAN-700?",
            "What remains unfinished in plugin 1.8.0?",
            "Что осталось в плагине 1.8.0?",
            "Show the history of the latest plugin improvements",
            "История последних улучшений плагина",
        ):
            with self.subTest(query=query):
                results = self.search(records, metadata, query, limit=1)
                self.assertEqual(["relevant"], [r["id"] for r in results])

    def test_latest_same_session_handoff_inherits_its_topic_relevance_position(self):
        records = [summary("intermediate", "topic"), summary("unrelated", "other"),
                   summary("final", "topic")]
        metadata = {"intermediate": event("2026-09-17T10:00:00Z"),
                    "final": event("2026-09-17T12:00:00Z"),
                    "unrelated": event("2026-09-19T10:00:00Z")}
        results = self.search(records, metadata, "What remains for the filtering work?")
        self.assertEqual(["final", "unrelated"], [r["id"] for r in results])

    def test_explicit_history_keeps_intermediate_handoffs_and_relevance(self):
        records = [summary("intermediate", "topic"), summary("final", "topic")]
        metadata = {"intermediate": event("2026-09-17T10:00:00Z", context_historical=True,
                                          later_summary_id="final"),
                    "final": event("2026-09-17T12:00:00Z")}
        for query in ("Покажи историю решений о фильтрации событий", "Show the history of event filtering",
                      "What did we decide before the final rollout?", "Что было решено до установки?"):
            with self.subTest(query=query):
                results = self.search(records, metadata, query)
                self.assertEqual(["intermediate", "final"], [r["id"] for r in results])
                self.assertTrue(results[0]["context_historical"])

    def test_duplicate_descriptive_notes_share_a_slot_but_changed_caveat_does_not(self):
        def note(entry_id, body):
            return {"id": entry_id, "kind": "note", "session_id": "same-session",
                    "source": "processor:test", "title": "Cache inspection " + entry_id,
                    "preview": body, "observation": {"type": "discovery", "narrative": body}}
        records = [note("old", "Cache keys are project-scoped. Runtime is unverified."),
                   note("repeat", "Cache keys are project-scoped. Runtime is unverified."),
                   note("changed", "Cache keys are project-scoped. Runtime check failed.")]
        metadata = {"old": event("2026-09-17T10:00:00Z"),
                    "repeat": event("2026-09-17T12:00:00Z")}
        results = self.search(records, metadata, "How does the cache work?", limit=2)
        self.assertEqual(["repeat", "changed"], [r["id"] for r in results])
        self.assertIn("failed", results[1]["preview"])
        history = self.search(records, metadata, "Show cache history")
        self.assertEqual(["old", "repeat", "changed"], [r["id"] for r in history])

    def test_identical_previews_do_not_collapse_records_with_unknown_tails(self):
        records = [{"id": entry_id, "kind": "note", "session_id": "same-session",
                    "source": "processor:test", "preview": "The same introductory preview…",
                    "observation": {"type": "discovery"}} for entry_id in ("one", "two")]
        self.assertEqual(["one", "two"], [r["id"] for r in self.search(records, {}, "cache")])

    def test_remaining_work_before_rollout_is_not_a_history_request(self):
        for query in ("What remains before the rollout?", "Что осталось сделать до релиза?",
                      "Что осталось незавершённым после последних улучшений плагина?"):
            with self.subTest(query=query):
                self.assertFalse(historical_query(query))


if __name__ == "__main__":
    unittest.main()
