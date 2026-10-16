"""Exact recall-audit questions with fictional, source-dated records."""
from pathlib import Path
import tempfile
import unittest

from codex_mem import semantic
from codex_mem.config import configure
from codex_mem.retrieval import current_state_scope, software_subject_matches
from codex_mem.store import Store
from tests.test_resume_current_state import Backend, CandidateStore, event, summary


VERSION_QUERY = "Какая версия Codex Mem установлена и чем она отличается от версии в README?"
JEV_QUERY = "Почему отключили Jev и какие проблемы памяти остались после обслуживания?"


class OctoberRecallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = str(Path(self.temp.name).resolve() / "codex-mem-public")

    def test_exact_installed_readme_question_recovers_latest_install_outside_window(self):
        old = summary("old", "old-session", title="Codex Mem 1.6.0 installed")
        current = {"id": "current", "project": self.project, "kind": "note",
                   "session_id": "new-session", "source": "processor:test",
                   "title": "Codex Mem 1.11.11 installed; README still describes 1.5.0",
                   "observation": {"type": "discovery"}}
        unrelated = summary("unrelated", "other-session", project=self.project,
                            title="README updated with invoice rounding examples")
        class RecentStore(CandidateStore):
            def retrieval_candidates(self, project, **kwargs):
                return [unrelated, current]
        store = RecentStore([old], {"old": event("2026-09-11T10:00:00Z"),
                                   "current": event("2026-10-11T10:00:00Z"),
                                   "unrelated": event("2026-10-12T10:00:00Z")})
        result = semantic.search(store, self.project, VERSION_QUERY, mode="semantic",
                                 intent="resume", limit=1, backend=Backend())
        self.assertEqual(["current"], [row["id"] for row in result["results"]])
        self.assertEqual("installation", current_state_scope(VERSION_QUERY, self.project))

    def test_installed_readme_scope_keeps_historical_version_and_extra_subject(self):
        for query in (
            VERSION_QUERY.replace("установлена", "раньше была установлена"),
            VERSION_QUERY.replace("Codex Mem", "Codex Mem 1.6.0"),
            VERSION_QUERY.replace("Codex Mem", "queue-worker"),
            VERSION_QUERY.replace("README?", "README по фильтрации Jev?"),
            VERSION_QUERY.replace("установлена", "установлена 11 октября"),
        ):
            with self.subTest(query=query):
                self.assertIsNone(current_state_scope(query, self.project))

    def test_install_receipt_precedes_newer_test_that_quotes_installed_query(self):
        receipt = {"id": "receipt", "kind": "note", "session_id": "install",
                   "title": "Three 1.11.11 installation roots matched",
                   "body": "The captured command reported runtime restarted to 1.11.11.",
                   "observation": {"type": "discovery"}}
        diagnostic = {"id": "diagnostic", "kind": "note", "session_id": "audit",
                      "title": "Retrieval regressions cover release, version, and handoff scope",
                      "body": "Tests assert the installed version query selects the current installed runtime.",
                      "observation": {"type": "discovery"}}
        store = CandidateStore([diagnostic, receipt], {
            "receipt": event("2026-10-11T04:00:00Z"),
            "diagnostic": event("2026-10-13T04:00:00Z")})
        result = semantic.search(store, self.project, VERSION_QUERY, mode="semantic",
                                 intent="resume", limit=1, backend=Backend())
        self.assertEqual(["receipt"], [row["id"] for row in result["results"]])

    def test_other_package_version_cannot_replace_named_project_installation(self):
        for query in (VERSION_QUERY, "Какая версия Codex Mem сейчас?"):
            with self.subTest(query=query):
                current = summary("current", "install", title="Codex Mem version 1.11.11 installed")
                dependency = {"id": "dependency", "project": self.project, "kind": "note",
                    "session_id": "lint", "source": "processor:test",
                    "title": "Installed Ruff version 0.14.0 dependency for linting",
                    "observation": {"type": "discovery"}}
                class RecentStore(CandidateStore):
                    def retrieval_candidates(self, project, **kwargs):
                        return [dependency]
                store = RecentStore([current], {"current": event("2026-10-11T04:00:00Z"),
                    "dependency": event("2026-10-12T04:00:00Z")})
                result = semantic.search(store, self.project, query, mode="semantic",
                    intent="resume", limit=5, backend=Backend())
                self.assertEqual(["current"], [row["id"] for row in result["results"]])

    def test_other_package_install_subject_does_not_require_version_or_english(self):
        for title in ("Ruff installation complete", "Установлен Ruff", "Ruff установлен",
                      "Installed Ruff 0.14.0 dependency for linting"):
            with self.subTest(title=title):
                self.assertFalse(software_subject_matches(title, self.project))

    def test_exact_jev_question_follows_latest_matching_handoff(self):
        old = summary("old", "maintenance", title="Jev review found queue concerns")
        current = summary("current", "maintenance", project=self.project,
            title="Global memory configuration and recovery check",
            body="JEV disabled globally. Pending captures and the protocol error remain.",
            source="processor:test")
        unrelated = summary("unrelated", "other-session", project=self.project,
            title="Jev SDK documentation examples", body="Read the current Jev SDK docs.")
        class LinkedStore(CandidateStore):
            def get(self, project, ids):
                self.linked_ids = ids
                return [dict(current)]
        store = LinkedStore([old, unrelated], {
            "old": event("2026-10-11T03:00:00Z", later_summary_id="current"),
            "current": event("2026-10-11T04:00:00Z"),
            "unrelated": event("2026-10-12T10:00:00Z")})
        result = semantic.search(store, self.project, JEV_QUERY, mode="semantic",
                                 intent="resume", limit=1, backend=Backend())
        self.assertEqual(["current"], [row["id"] for row in result["results"]])
        self.assertEqual(["current"], store.linked_ids)

    def test_jev_followup_preserves_filters_history_and_extra_topics(self):
        class NoReadStore(CandidateStore):
            def get(self, project, ids):
                raise AssertionError("Constrained queries must not hydrate continuation links")
        for query, filters in (
            (JEV_QUERY, {"kinds": ["session_summary"]}),
            (JEV_QUERY + " Раньше?", {}),
            (JEV_QUERY + " В версии 1.6.0?", {}),
            (JEV_QUERY + " Для PRM-934?", {}),
        ):
            with self.subTest(query=query, filters=filters):
                store = NoReadStore([summary("old", "maintenance", title="Jev review")],
                    {"old": event("2026-10-11T03:00:00Z", later_summary_id="current")})
                semantic.search(store, self.project, query, mode="semantic", intent="resume",
                                backend=Backend(), **filters)

    def test_exact_issue_id_does_not_match_split_metadata_or_source_ids(self):
        data = Path(self.temp.name) / "data"
        configure(data, semantic_enabled=False, processor_enabled=False, service_enabled=False)
        with Store(data) as store:
            store.remember(self.project, "Unrelated roadmap review", "No task decision.",
                kind="session_summary", session_summary={"learned": "Reviewed pm-prm.",
                    "notes": "Receipt 019934abcdef."})
            for query in ("PRM-934", "Зависит ли работа с черными списками PRM-934 от событий позиций PRM-941?"):
                with self.subTest(query=query):
                    self.assertEqual([], store.search(self.project, query))
            target = store.remember(self.project, "PRM-934 decision", "Blacklist scope recorded.")
            self.assertEqual([target["id"]], [r["id"] for r in store.search(self.project, "PRM-934")])

    def test_dependency_installs_do_not_fill_recent_project_install_window(self):
        data = Path(self.temp.name) / "data"
        configure(data, semantic_enabled=False, processor_enabled=False, service_enabled=False)
        with Store(data) as store:
            target = store.remember(self.project, "Codex Mem 1.11.11 installed",
                "The runtime restart was reported.", kind="session_summary", session_id="install")
            store._connection.execute("UPDATE entries SET created_at=? WHERE id=?",
                ("2026-10-11T04:00:00Z", target["id"]))
            for index in range(20):
                store.remember(self.project, f"Installed Ruff version 0.14.{index} dependency",
                    "The linter package was installed.", kind="session_summary", session_id=f"lint-{index}")
            pool = store.retrieval_candidates(self.project, query=VERSION_QUERY)
            self.assertEqual([target["id"]], [row["id"] for row in pool])


if __name__ == "__main__":
    unittest.main()
