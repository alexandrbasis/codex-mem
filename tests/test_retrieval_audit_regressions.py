"""Regressions from the Sep 22 live retrieval audit, with fictional records."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from codex_mem import semantic
from codex_mem.config import configure
from codex_mem.retrieval import current_state_scope, preview_records, prompt_match_score, prompt_query_plan
from codex_mem.store import Store
from tests.test_resume_current_state import Backend, CandidateStore, event, summary


AUDIT_QUERIES = (
    "Как сейчас работает codex-mem и что осталось проверить?",
    "Что вошло в последний релиз и чем подтверждено, что его выкатили?",
    "Что у нас осталось непроверенным после последнего релиза?",
    "проанализируй как там наш плагин как справляется с задачей своей",
    "Какая версия codex-mem сейчас установлена и что подтвердило её работу?",
    "Что сейчас с плагином codex-mem, что уже исправили и что осталось сделать?",
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
                    body="The codex-mem release was installed and pushed; native integration remains unverified.",
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

    def test_installed_state_keeps_specific_subjects_and_history(self):
        for query in (
            "Какая версия codex-mem 1.6.0 сейчас установлена?",
            "Какая версия memory_search сейчас установлена?",
            "Какая версия queue-worker сейчас установлена?",
            "Какая версия codex-mem раньше была установлена?",
            "Что сейчас с плагином codex-mem и фильтрацией Jev?",
            "Что сейчас исправили в codex_mem/retrieval.py?",
        ):
            with self.subTest(query=query):
                self.assertIsNone(current_state_scope(query, self.project))

    def test_live_state_phrases_prefer_october_install_over_september_version(self):
        for query in AUDIT_QUERIES[-2:]:
            with self.subTest(query=query):
                records = [
                    summary("september", "old-session", title="Codex Mem 1.6.0 installed",
                        body="The old plugin installation passed its check.",
                        fixture_time="2026-09-10T10:00:00Z"),
                    summary("october", "current-session", title="Codex Mem 1.11.8 installed",
                        body="Queue recovery and real-session search checks passed.",
                        fixture_time="2026-10-10T10:00:00Z"),
                ]
                self.assertEqual(["october"], [row["id"] for row in self.search(query, records, limit=1)])

    def test_installed_version_prioritizes_new_install_finding_over_old_handoff(self):
        records = [
            summary("old", "old-session", title="Version 1.6.0 installed",
                fixture_time="2026-09-10T10:00:00Z"),
            {"id": "current", "kind": "note", "session_id": "install-session",
             "title": "Install report showed runtime 1.11.8", "body": "Installed runtime verified.",
             "observation": {"type": "discovery"}, "fixture_time": "2026-10-10T10:00:00Z"},
        ]
        self.assertEqual(["current"], [row["id"] for row in self.search(AUDIT_QUERIES[-2], records, limit=1)])

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
        context = self.store.context(self.project, query=query, budget=6000, allow_remote=False)
        return [row.attrib["id"] for row in ET.fromstring(context).findall("entry")]

    def test_automatic_context_uses_current_evidence_across_language(self):
        self.note("Плагин Codex Mem: старый релиз", "Плагин памяти задерживает обработку.",
                  "old", 14, session_summary={"next_steps": "Проверить обработку памяти."})
        latest = self.note("Release verification completed", "The codex-mem release was installed and pushed.",
                          "current", 21, session_summary={"completed": "CI passed.",
                          "next_steps": "Native integration remains unverified."})
        for query in AUDIT_QUERIES:
            with self.subTest(query=query):
                self.assertEqual(latest["id"], self.ids(query)[0])

    def test_russian_hook_timeout_question_finds_only_requested_release(self):
        query = "Что решили и почему по таймауту хуков в 1.9.1?"
        self.note("Hook timeout in 1.8.0", "The old hook deadline was investigated.", "old", 14)
        target = self.note("Hook timeout decision in 1.9.1",
            "We chose a supervised hook worker and a two second internal deadline because the host kills slow hooks.",
            "target", 28)
        self.note("Hook timeout decision in 1.9.2", "A later release changed the worker.", "new", 29)
        self.assertEqual([target["id"]], self.ids(query))
        self.assertEqual([target["id"]], [row["id"] for row in self.store.search(self.project, query)])

    def test_russian_causal_preposition_does_not_require_literal_hyphen(self):
        target = self.note("Jev: обработка памяти застревала",
            "Память застревала при Jev. Очередь исправили и проверили обработку.", "target", 28)
        self.note("Invoice parser", "Reviewed VAT rounding.", "other", 29)
        query = "Почему память застревала из-за Jev и как это исправили?"
        self.assertEqual([target["id"]], [row["id"] for row in self.store.search(self.project, query)])
        self.assertEqual([target["id"]], self.ids(query))
        self.assertEqual([], prompt_query_plan(query)["identifiers"])

    def test_broad_resume_recovers_recent_evidence_missing_from_relevance_window(self):
        old = self.note("Codex Mem 1.6.0 installed", "Old install verification.", "old", 10)
        current = self.note("Install report showed runtime 1.11.8",
            "Queue recovery and real-session search checks passed.", "current", 28)
        newer_unrelated = self.note("Invoice parser review", "VAT rounding passed.", "other", 29)
        for mode in ("lexical", "semantic", "hybrid", "auto"):
            with self.subTest(mode=mode), patch.object(semantic, "_lexical_results", return_value=[old]), \
                    patch.object(semantic, "_semantic_results", return_value=[old]), \
                    patch.object(semantic, "_resume_candidates", return_value=[]):
                result = semantic.search(self.store, self.project, AUDIT_QUERIES[-2],
                    mode=mode, intent="resume", limit=5, backend=Backend())
                self.assertIn(current["id"], [row["id"] for row in result["results"]])
                self.assertNotIn(newer_unrelated["id"], [row["id"] for row in result["results"]])
                self.assertGreater(result["resume_added_candidates"], 0)

    def test_recent_resume_expansion_keeps_release_scope_and_opt_outs(self):
        release = self.note("Release checks passed", "Release 1.11.8 shipped.", "release", 28)
        self.note("Invoice parser review", "VAT rounding passed.", "other", 29)
        with patch.object(semantic, "_lexical_results", return_value=[]), \
                patch.object(semantic, "_resume_candidates", return_value=[]):
            result = semantic.search(self.store, self.project, AUDIT_QUERIES[1],
                mode="lexical", intent="resume", limit=5)
        self.assertEqual([release["id"]], [row["id"] for row in result["results"]])
        with patch.object(self.store, "retrieval_candidates", side_effect=AssertionError("No recent expansion")):
            for query, intent, filters in (
                (AUDIT_QUERIES[-2], "lookup", {}),
                ("Какая версия codex-mem 1.6.0 сейчас установлена?", "resume", {}),
                ("Покажи историю версий codex-mem", "resume", {}),
                ("Что осталось проверить в фильтрации Jev?", "resume", {}),
                (AUDIT_QUERIES[-2], "resume", {"files": ["worker.py"]}),
            ):
                semantic.search(self.store, self.project, query, mode="lexical", intent=intent, **filters)

    def test_installation_scope_filters_recent_inspection_before_candidate_cap(self):
        target = self.note("Install report showed runtime 1.11.8",
            "The plugin was installed; CI verification remained pending.", "install", 10)
        for day in range(11, 30):
            self.note("Inspected worker leases and health checks",
                "Source inspection found expired leases; no behavior was changed.", f"inspect-{day}", day)
        query = AUDIT_QUERIES[-2]
        pool = self.store.retrieval_candidates(self.project, query=query)
        self.assertEqual([target["id"]], [row["id"] for row in pool])
        with patch.object(semantic, "_lexical_results", return_value=[]), \
                patch.object(semantic, "_resume_candidates", return_value=[]):
            result = semantic.search(self.store, self.project, query, mode="lexical", intent="resume", limit=5)
        self.assertEqual([target["id"]], [row["id"] for row in result["results"]])
        broad = semantic.search(self.store, self.project, AUDIT_QUERIES[-1],
            mode="lexical", intent="resume", limit=5)
        self.assertIn("Inspected worker leases", broad["results"][0]["title"])

    def test_causal_preposition_preserves_technical_hyphen_constraints(self):
        for identifier in ("queue-worker", "AIAN-700", "очередь-воркер"):
            query = f"Почему память застревала из-за {identifier} и как это исправили?"
            plan = prompt_query_plan(query)
            self.assertEqual([identifier.casefold()], plan["identifiers"])
            self.assertEqual(0, prompt_match_score("Память застревала. Очередь исправили.", plan))
            self.assertGreater(prompt_match_score(
                f"Память застревала при {identifier}; очередь исправили.", plan), 0)

    def test_semantic_and_hybrid_enforce_requested_version_before_ranking(self):
        query = "Что решили и почему по таймауту хуков в 1.9.1?"
        old = self.note("Hook timeout decision in 1.8.0", "Old worker implementation.", "old", 14)
        target = self.note("Hook timeout decision in 1.9.1", "Supervised worker fixed the hook deadline.",
                           "target", 28)
        batch = self.store.claim_embedding_batch(
            self.project, semantic.MODEL, semantic.MODEL_REVISION, semantic.DIMENSIONS)
        self.assertIsNotNone(batch)
        self.store.complete_embedding_batch(self.project, batch["job_id"], batch["lease_token"],
            vectors=[{"entry_id": item["id"], "content_hash": item["content_hash"],
                      "vector": ([1.0, 0.0] if item["id"] == old["id"] else [0.0, 1.0])
                                + [0.0] * (semantic.DIMENSIONS - 2)} for item in batch["entries"]])
        for mode in ("semantic", "hybrid", "auto"):
            with self.subTest(mode=mode):
                result = semantic.search(self.store, self.project, query, mode=mode,
                    backend=Backend(), limit=5)
                self.assertEqual([target["id"]], [row["id"] for row in result["results"]])

    def test_named_project_in_broad_state_question_is_scope_not_literal(self):
        target = self.note("Hook worker status", "The latest check remains pending.", "target", 28)
        batch = self.store.claim_embedding_batch(
            self.project, semantic.MODEL, semantic.MODEL_REVISION, semantic.DIMENSIONS)
        self.store.complete_embedding_batch(self.project, batch["job_id"], batch["lease_token"],
            vectors=[{"entry_id": item["id"], "content_hash": item["content_hash"],
                      "vector": [1.0] + [0.0] * (semantic.DIMENSIONS - 1)}
                     for item in batch["entries"]])
        result = semantic.search(self.store, self.project,
            "Как сейчас работает codex-mem и что осталось проверить?",
            mode="semantic", backend=Backend(), limit=5)
        self.assertEqual([target["id"]], [row["id"] for row in result["results"]])

    def test_version_and_extra_topic_cannot_be_relaxed_by_natural_fallback(self):
        self.note("Hook timeout decision in 1.9.1", "Supervised worker fixed hook timeouts.",
                  "target", 28)
        for query in ("Что решили и почему по таймауту хуков и биллингу в 1.9.1?",
                      "Что решили и почему по таймауту хуков в 1.9.10?"):
            with self.subTest(query=query):
                self.assertEqual([], self.ids(query))
                self.assertEqual([], self.store.search(self.project, query))
        plan = prompt_query_plan("Что решили и почему по таймауту хуков в 1.9.1?")
        self.assertIsNotNone(plan)
        self.assertEqual(0, prompt_match_score("Hook timeout decision in 1.9.10", plan))

    def test_natural_fallback_keeps_project_and_raw_source_boundaries(self):
        query = "Что решили и почему по таймауту хуков в 1.9.1?"
        self.store.remember(self.project / "foreign", "Hook timeout decision in 1.9.1",
                            "The unrelated project chose a worker.")
        self.store.remember(self.project, "Hook timeout decision in 1.9.1",
                            "Raw source event only.", source="hook:Stop")
        self.assertEqual([], self.ids(query))
        self.assertEqual([], self.store.search(self.project, query))

    def test_overflowed_topical_terms_do_not_drop_the_last_constraint(self):
        topics = ("atlas", "beryl", "cedar", "delta", "elm", "fable", "glade",
                  "harbor", "iris", "juniper", "keel", "lotus", "maple",
                  "north", "opal", "pine", "quartz")
        self.note(" ".join((*topics[:-1], "1.9.1")), "A different decision.", "target", 28)
        query = "What about " + " ".join(topics) + " in 1.9.1?"
        self.assertIsNone(prompt_query_plan(query))
        self.assertEqual([], self.ids(query))
        self.assertEqual([], self.store.search(self.project, query))

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
