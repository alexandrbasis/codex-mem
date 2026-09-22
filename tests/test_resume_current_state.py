"""Current-state selection stays bounded by the original query candidates."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_mem import semantic


class Backend:
    ready = True
    unavailable_code = None

    def split(self, text, *, prefix=""):
        return [prefix + text]

    def embed(self, texts):
        return [[1.0] + [0.0] * (semantic.DIMENSIONS - 1) for _ in texts]


class CandidateStore:
    """Already-ranked query matches with realistic candidate filters."""

    def __init__(self, records, metadata):
        self.records = records
        self.metadata = metadata
        self.metadata_calls = []
        self.search_calls = []

    def _matches(self, project, *, limit, kinds=None, types=None, concepts=None, files=None):
        self.search_calls.append((project, kinds, types, concepts, files))
        matches = []
        for record in self.records:
            if record.get("project", project) != project or not record.get("active", True):
                continue
            if record.get("kind") in {"tool", "session", "checkpoint"}:
                continue
            if not record.get("query_match", True):
                continue
            if kinds is not None and record.get("kind") not in kinds:
                continue
            observation = record.get("observation", {})
            if types is not None and observation.get("type") not in types:
                continue
            if concepts is not None and not set(concepts).intersection(observation.get("concepts", [])):
                continue
            if files is not None and not set(files).intersection(observation.get("files", [])):
                continue
            matches.append(record)
        return matches[:limit]

    def search(self, project, query, **kwargs):
        return self._matches(project, **kwargs)

    def semantic_search(self, project, vector, model, revision, dimensions, **kwargs):
        return self._matches(project, **kwargs)

    def resume_metadata(self, project, ids):
        self.metadata_calls.append((project, list(ids)))
        # A storage adapter returning extra metadata must not add candidates.
        return dict(self.metadata)


def summary(entry_id, session="session-one", **extra):
    return {"id": entry_id, "kind": "session_summary", "session_id": session, **extra}


def event(at, **extra):
    return {"event_at": at, "event_id": "source-" + at, "event_time_basis": "source_event", **extra}


class ResumeCurrentStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name).resolve() / "project"
        self.project.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def search(self, store, *, mode="lexical", limit=5, **kwargs):
        return semantic.search(
            store, self.project, "release current status", mode=mode,
            intent="resume", limit=limit, backend=Backend(), **kwargs,
        )

    def test_completed_followup_replaces_better_matched_delegation_in_each_mode(self):
        records = [
            summary("delegation", title="Release work delegated"),
            {"id": "decision", "kind": "decision"},
            summary("completed", title="Release completed and checked"),
        ]
        metadata = {
            "delegation": event("2026-09-16T10:00:00Z", context_historical=True, later_summary_id="completed"),
            "completed": event("2026-09-16T12:00:00Z"),
        }
        for mode in ("lexical", "semantic", "hybrid", "auto"):
            with self.subTest(mode=mode):
                store = CandidateStore(records, metadata)
                result = self.search(store, mode=mode, limit=2)
                self.assertEqual(["completed", "decision"], [item["id"] for item in result["results"]])
                self.assertEqual("source_event", result["results"][0]["event_time_basis"])
                self.assertEqual(1, len(store.metadata_calls))
                self.assertEqual(str(self.project), store.metadata_calls[0][0])

    def test_delayed_older_summary_write_does_not_beat_newer_source_event(self):
        store = CandidateStore(
            [summary("delayed-old", created_at="2026-09-17T20:00:00Z"),
             summary("current", created_at="2026-09-16T12:00:00Z")],
            {"delayed-old": event("2026-09-15T10:00:00Z"),
             "current": event("2026-09-16T11:00:00Z")},
        )
        result = self.search(store)
        self.assertEqual(["current"], [item["id"] for item in result["results"]])

    def test_completion_below_old_twenty_candidate_window_is_selected(self):
        class SemanticOnlyStore(CandidateStore):
            def search(self, project, query, **kwargs):
                # Natural-language similarity can match a release summary
                # even when none of the query's words match lexically.
                return []

        records = [{"id": f"detail-{index}", "kind": "note"} for index in range(60)]
        records[11] = summary("old-delegation")
        records[52] = summary("completed")
        metadata = {
            "old-delegation": event("2026-09-15T10:00:00Z", context_historical=True,
                                    later_summary_id="completed"),
            "completed": event("2026-09-16T11:00:00Z"),
        }
        for mode in ("semantic", "hybrid", "auto"):
            with self.subTest(mode=mode):
                store = SemanticOnlyStore(records, metadata)
                result = self.search(store, mode=mode, limit=5)
                self.assertEqual("completed", result["results"][0]["id"])
                self.assertEqual(5, len(result["results"]))
                self.assertNotIn("old-delegation", [item["id"] for item in result["results"]])

    def test_different_session_summaries_keep_relevance_finding_balance_and_priority(self):
        store = CandidateStore(
            [summary("release-1.4", "old-session"),
             {"id": "detail", "kind": "note"},
             {"id": "fix", "kind": "bugfix"},
             summary("release-1.9", "new-session")],
            {"release-1.4": event("2026-08-01T10:00:00Z"),
             "release-1.9": event("2026-09-16T11:00:00Z")},
        )
        result = semantic.search(store, self.project, "release signing current status",
                                 mode="lexical", intent="resume", limit=4)
        self.assertEqual(
            ["release-1.4", "fix", "release-1.9", "detail"],
            [item["id"] for item in result["results"]],
        )

    def test_latest_summary_on_other_topic_keeps_matching_history_visible(self):
        store = CandidateStore(
            [summary("matching-old", title="Release completed"),
             summary("unrelated-new", title="Billing followup", query_match=False)],
            {"matching-old": event("2026-09-15T11:00:00Z", context_historical=True,
                                   later_summary_id="unrelated-new"),
             "unrelated-new": event("2026-09-16T11:00:00Z")},
        )
        result = self.search(store)
        self.assertEqual(["matching-old"], [item["id"] for item in result["results"]])
        self.assertIs(True, result["results"][0]["context_historical"])
        self.assertEqual("unrelated-new", result["results"][0]["later_summary_id"])
        self.assertNotIn("unrelated-new", store.metadata_calls[0][1])
        self.assertNotIn("current", result["results"][0])

    def test_missing_session_ids_are_independent_and_ties_preserve_relevance(self):
        records = [summary("one", None), summary("two", None), summary("three", "")]
        store = CandidateStore(records, {item["id"]: event("2026-09-16T11:00:00Z") for item in records})
        result = self.search(store)
        self.assertEqual(["one", "two", "three"], [item["id"] for item in result["results"]])

    def test_missing_or_invalid_telemetry_preserves_relevance_not_created_at(self):
        for metadata in ({}, {"later-write": {"event_at": "invalid", "event_time_basis": "source_event"}}):
            with self.subTest(metadata=metadata):
                store = CandidateStore(
                    [summary("relevant", created_at="2026-08-01T10:00:00Z"),
                     summary("later-write", created_at="2026-09-16T11:00:00Z")],
                    metadata,
                )
                result = self.search(store)
                self.assertEqual(["relevant"], [item["id"] for item in result["results"]])

    def test_source_less_summaries_use_recorded_time_with_explicit_basis(self):
        store = CandidateStore(
            [summary("old-manual"), summary("new-manual")],
            {"old-manual": {"event_at": "2026-09-15T11:00:00Z", "event_time_basis": "recorded_at"},
             "new-manual": {"event_at": "2026-09-16T11:00:00Z", "event_time_basis": "recorded_at"}},
        )
        result = self.search(store)
        self.assertEqual(["new-manual"], [item["id"] for item in result["results"]])
        self.assertEqual("recorded_at", result["results"][0]["event_time_basis"])

    def test_event_time_compares_instants_across_timezones(self):
        store = CandidateStore(
            [summary("earlier-local"), summary("later-utc")],
            {"earlier-local": event("2026-09-16T13:00:00+03:00"),
             "later-utc": event("2026-09-16T11:00:00Z")},
        )
        self.assertEqual("later-utc", self.search(store)["results"][0]["id"])

    def test_metadata_requests_are_bounded_after_priority_expansion(self):
        records = [{"id": f"note-{index}", "kind": "note"} for index in range(100)]
        records.extend(summary(f"handoff-{index}", f"session-{index}") for index in range(50))
        store = CandidateStore(records, {})
        self.search(store, limit=50)
        self.assertEqual([100, 50], [len(ids) for _, ids in store.metadata_calls])
        requested_ids = [entry_id for _, ids in store.metadata_calls for entry_id in ids]
        self.assertEqual(150, len(set(requested_ids)))

    def test_lookup_keeps_relevance_and_does_not_read_resume_metadata(self):
        records = [summary("delegation"), summary("completed")]
        store = CandidateStore(records, {"completed": event("2026-09-16T11:00:00Z")})
        result = semantic.search(store, self.project, "release", mode="lexical")
        self.assertEqual(records, result["results"])
        self.assertEqual([], store.metadata_calls)
        self.assertNotIn("resume_selection", result)

    def test_metadata_cannot_add_other_project_inactive_raw_or_filtered_records(self):
        selected = summary("selected", observation={"type": "bugfix", "concepts": ["release"],
                                                    "files": ["codex_mem/semantic.py"]})
        records = [
            summary("other-project", project="/another/project"),
            summary("inactive", active=False),
            {"id": "raw", "kind": "tool"},
            summary("other-kind", kind="note"),
            summary("other-type", observation={"type": "decision"}),
            summary("other-concept", observation={"type": "bugfix", "concepts": ["billing"]}),
            summary("other-file", observation={"type": "bugfix", "concepts": ["release"], "files": ["x.py"]}),
            selected,
        ]
        metadata = {item["id"]: event("2026-09-16T11:00:00Z") for item in records}
        store = CandidateStore(records, metadata)
        result = self.search(store, mode="hybrid", kinds=["session_summary"], types=["bugfix"],
                             concepts=["release"], files=["codex_mem/semantic.py"])
        self.assertEqual(["selected"], [item["id"] for item in result["results"]])
        self.assertEqual([(str(self.project), ["selected"])], store.metadata_calls)
        for project, kinds, types, concepts, files in store.search_calls:
            self.assertEqual(str(self.project), project)
            self.assertEqual(["session_summary"], kinds)
            self.assertEqual(["bugfix"], types)
            self.assertEqual(["release"], concepts)
            self.assertEqual(["codex_mem/semantic.py"], files)


if __name__ == "__main__":
    unittest.main()
