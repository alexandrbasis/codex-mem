from __future__ import annotations

from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_mem.config import configure
from codex_mem.observer_usage_store import begin_attempt, finish_attempt
from codex_mem.service import enqueue
from codex_mem.store import Store, project_key
from codex_mem.usage_store import UsageStore


HELPER_PATH = Path(__file__).parents[1] / "skills" / "maintenance" / "scripts" / "health_check.py"
SPEC = importlib.util.spec_from_file_location("codex_mem_health_check", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
health_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(health_check)


class MaintenanceHealthCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "memory-home"
        self.project_a = self.root / "project-a"
        self.project_b = self.root / "project-b"
        self.project_a.mkdir()
        self.project_b.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def report(self, project: Path | None = None, **kwargs: object) -> dict[str, object]:
        return health_check.collect_report(
            project or self.project_a,
            data_dir=self.data_dir,
            now=1_767_244_800.0,  # 2026-01-01T05:20:00Z
            **kwargs,
        )

    def test_missing_home_is_read_only_and_defaults_to_idle(self) -> None:
        missing = self.root / "not-created"
        before = sorted(path.name for path in self.root.iterdir())

        report = health_check.collect_report(
            self.project_a,
            data_dir=missing,
            now=1_767_244_800.0,
        )

        self.assertEqual("idle", report["status"])
        self.assertEqual("missing", report["storage"]["status"])
        self.assertEqual("default", report["configuration"]["status"])
        self.assertFalse(missing.exists())
        self.assertEqual(before, sorted(path.name for path in self.root.iterdir()))

    def test_existing_database_and_config_are_not_changed(self) -> None:
        configure(self.data_dir, capture_scope="all")
        with Store(self.data_dir) as store:
            store.remember(
                self.project_a,
                "Local note",
                "A private body that must never be emitted by diagnostics.",
                source="manual:test",
            )
        tracked = [self.data_dir / name for name in ("memory.sqlite3", "config.json")]
        before = {path: (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes()) for path in tracked}

        report = self.report(deep=True)

        self.assertEqual("healthy", report["storage"]["integrity"]["status"])
        for path, state in before.items():
            self.assertEqual(state, (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes()))
        self.assertEqual("all", report["capture_scope"])

    def test_raw_observations_are_not_counted_as_pending_semantic_memories(self) -> None:
        configure(self.data_dir, capture_scope="all")
        with Store(self.data_dir) as store:
            store.remember(self.project_a, "Raw tool", "Ran maintenance", source="hook:PostToolUse:call-1")
            store.remember(self.project_a, "Decision", "Use project scoped locks")
        report = self.report()
        project = report["projects"][0]
        self.assertEqual(2, project["records"]["entries"])
        self.assertEqual(1, project["semantic"]["pending"])
        self.assertEqual(1, project["semantic"]["raw_observations_excluded"])

    def test_corrupt_database_and_config_report_codes_without_raw_content(self) -> None:
        self.data_dir.mkdir()
        (self.data_dir / "memory.sqlite3").write_bytes(b"not a sqlite database PRIVATE_SENTINEL")
        (self.data_dir / "config.json").write_text(
            '{"included_projects":"PRIVATE_SENTINEL","token":"PRIVATE_SENTINEL"}',
            encoding="utf-8",
        )

        report = self.report()
        rendered = json.dumps(report, sort_keys=True)

        self.assertEqual("unavailable", report["status"])
        self.assertIn("database_unreadable", report["errors"])
        self.assertIn("config_invalid", report["errors"])
        self.assertNotIn("PRIVATE_SENTINEL", rendered)

    def test_corrupt_service_snapshot_is_degraded_without_repair(self) -> None:
        self.data_dir.mkdir()
        state_path = self.data_dir / "service-state.json"
        state_path.write_text('{"version": "bad", "projects": {}}', encoding="utf-8")
        before = state_path.read_bytes()

        report = self.report()

        self.assertEqual("degraded", report["status"])
        self.assertIn("service_state_invalid", report["errors"])
        self.assertEqual(before, state_path.read_bytes())

    def _insert_job(
        self,
        entry_id: str,
        *,
        status: str,
        created: str,
        updated: str,
        lease: str | None = None,
        completed: str | None = None,
        attempts: int = 1,
        project: Path | None = None,
    ) -> None:
        database = self.data_dir / "memory.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """
                INSERT INTO observation_jobs(
                    id, project, processor_id, model, reasoning_effort, session_id,
                    input_fingerprint, input_limit, status, disposition, lease_token,
                    lease_expires_at, attempt_count, worker_thread_id, worker_turn_id,
                    error_code, output_ids_json, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, ?, '[]', ?, ?, ?)
                """,
                (
                    f"job-{status}-{entry_id[:8]}",
                    project_key(project or self.project_a),
                    "codex-mem-native-observation-v1",
                    "gpt-5.6-luna",
                    "medium",
                    "session-1",
                    (entry_id * 2)[:64],
                    12000,
                    status,
                    "lease-token" if lease else None,
                    lease,
                    attempts,
                    "failed-code" if status == "failed" else None,
                    created,
                    updated,
                    completed,
                ),
            )
            connection.execute(
                "INSERT INTO observation_job_sources(job_id, source_id) VALUES (?, ?)",
                (f"job-{status}-{entry_id[:8]}", entry_id),
            )

    def test_idle_project_is_distinct_from_stale_queue(self) -> None:
        with Store(self.data_dir) as store:
            failed = store.remember(
                self.project_a,
                "Raw event",
                "private observation body",
                kind="session",
                source="hook:Stop",
            )
            stale = store.remember(
                self.project_a,
                "Raw event two",
                "private observation body two",
                kind="session",
                source="hook:UserPromptSubmit",
            )
        self._insert_job(
            failed["id"],
            status="failed",
            created="2026-01-01T00:00:00Z",
            updated="2026-01-01T01:00:00Z",
        )
        self._insert_job(
            stale["id"],
            status="running",
            created="2026-01-01T00:00:00Z",
            updated="2026-01-01T01:00:00Z",
            lease="2025-12-31T00:00:00Z",
        )

        stale_report = self.report(self.project_a)
        project_data = stale_report["projects"][0]
        self.assertEqual("stale", project_data["status"])
        self.assertEqual(1, project_data["observation_queue"]["counts"]["failed"])
        self.assertEqual(1, project_data["observation_queue"]["stale_running"])
        self.assertEqual("gpt-5.6-luna", project_data["processing"]["model_profiles"][0]["model"])
        self.assertEqual("medium", project_data["processing"]["model_profiles"][0]["reasoning_effort"])

        with Store(self.data_dir) as store:
            store.remember(self.project_b, "Idle note", "separate project body", source="manual:test")
        idle_report = self.report(self.project_b)
        self.assertEqual("healthy", idle_report["projects"][0]["status"])
        self.assertEqual("idle", idle_report["projects"][0]["observation_queue"]["status"])

    def test_active_processing_is_not_blocked_by_historical_quarantine(self) -> None:
        with Store(self.data_dir) as store:
            failed = store.remember(
                self.project_a, "Rejected batch", "private rejected body",
                kind="session", source="hook:Stop", session_id="old-session",
            )
            processed = store.remember(
                self.project_a, "Completed batch", "private completed body",
                kind="session", source="hook:Stop", session_id="completed-session",
            )
            running = store.remember(
                self.project_a, "Active batch", "private active body",
                kind="session", source="hook:Stop", session_id="active-session",
            )
        self._insert_job(
            failed["id"], status="failed", created="2025-12-29T00:00:00Z",
            updated="2025-12-29T01:00:00Z",
        )
        self._insert_job(
            processed["id"], status="processed", created="2026-01-01T04:00:00Z",
            updated="2026-01-01T04:10:00Z", completed="2026-01-01T04:10:00Z",
        )
        self._insert_job(
            running["id"], status="running", created="2026-01-01T05:19:00Z",
            updated="2026-01-01T05:19:30Z", lease="2026-01-01T05:30:00Z",
        )
        configure(self.data_dir, capture_scope="all")
        enqueue(self.project_a, self.data_dir, clock=lambda: 1_767_244_799.0)

        report = self.report(self.project_a)
        project = report["projects"][0]
        queue = project["observation_queue"]

        self.assertEqual("in_progress", report["status"])
        self.assertEqual(2, report["schema_version"])
        self.assertEqual("in_progress", project["status"])
        self.assertEqual("in_progress", queue["status"])
        self.assertEqual(1, queue["counts"]["failed"])
        self.assertEqual(1, queue["quarantined_jobs"])
        self.assertEqual([{"code": "failed-code", "jobs": 1}], queue["failure_codes"])
        self.assertEqual(0, queue["pending_sources"])
        self.assertEqual("2025-12-29T01:00:00Z", queue["latest_failed_at"])
        self.assertEqual(0, project["observations"]["blocked_observations"])
        self.assertEqual(1, project["observations"]["quarantined_observations"])
        self.assertFalse(project["service"]["queue_metadata"]["blocked"])

    def test_service_block_is_reported_even_with_historical_success(self) -> None:
        with Store(self.data_dir) as store:
            failed = store.remember(
                self.project_a, "Current failed batch", "private failed body",
                kind="session", source="hook:Stop", session_id="failed-session",
            )
            processed = store.remember(
                self.project_a, "Earlier completed batch", "private completed body",
                kind="session", source="hook:Stop", session_id="completed-session",
            )
        self._insert_job(
            processed["id"], status="processed", created="2026-01-01T03:00:00Z",
            updated="2026-01-01T03:10:00Z", completed="2026-01-01T03:10:00Z",
        )
        self._insert_job(
            failed["id"], status="failed", created="2026-01-01T05:00:00Z",
            updated="2026-01-01T05:10:00Z",
        )
        configure(self.data_dir, capture_scope="all")
        enqueue(self.project_a, self.data_dir, clock=lambda: 1_767_244_000.0)
        state_path = self.data_dir / "service-state.json"
        state = json.loads(state_path.read_text())
        state["projects"][project_key(self.project_a)]["blocked"] = True
        state["projects"][project_key(self.project_a)]["last_code"] = "invalid_response"
        state_path.write_text(json.dumps(state))

        report = self.report(self.project_a)
        project = report["projects"][0]

        self.assertEqual("blocked", report["status"])
        self.assertEqual("blocked", project["status"])
        self.assertEqual("blocked", project["observation_queue"]["status"])
        self.assertEqual(1, project["observations"]["blocked_observations"])
        self.assertEqual(0, project["observations"]["quarantined_observations"])

    def test_quarantined_batch_and_resolved_batch_have_distinct_statuses(self) -> None:
        with Store(self.data_dir) as store:
            source = store.remember(
                self.project_a, "Rejected batch", "private rejected body",
                kind="session", source="hook:Stop", session_id="failed-session",
            )
        self._insert_job(
            source["id"], status="failed", created="2026-01-01T03:00:00Z",
            updated="2026-01-01T03:10:00Z",
        )

        quarantined = self.report(self.project_a)
        project = quarantined["projects"][0]
        self.assertEqual("quarantined", quarantined["status"])
        self.assertEqual("quarantined", project["status"])
        self.assertEqual("quarantined", project["observation_queue"]["status"])
        self.assertEqual(1, project["observations"]["quarantined_observations"])

        with closing(sqlite3.connect(self.data_dir / "memory.sqlite3")) as connection, connection:
            connection.execute(
                """UPDATE observation_jobs SET status = 'processed', disposition = 'processed',
                   error_code = NULL, updated_at = ?, completed_at = ? WHERE project = ?""",
                ("2026-01-01T04:10:00Z", "2026-01-01T04:10:00Z", project_key(self.project_a)),
            )

        healthy = self.report(self.project_a)
        project = healthy["projects"][0]
        self.assertEqual("healthy", healthy["status"])
        self.assertEqual("healthy", project["status"])
        self.assertEqual("healthy", project["observation_queue"]["status"])
        self.assertEqual(0, project["observations"]["failed_observations"])

    def test_service_enqueue_is_reported_as_overdue_without_starting_worker(self) -> None:
        configure(self.data_dir, capture_scope="all")
        self.assertEqual("queued", enqueue(self.project_a, self.data_dir, clock=lambda: 1.0)["status"])
        state_path = self.data_dir / "service-state.json"
        before = state_path.read_bytes()

        report = self.report(self.project_a)
        project_data = report["projects"][0]

        self.assertEqual("stale", project_data["status"])
        self.assertEqual("stopped", project_data["service"]["worker_liveness"])
        self.assertTrue(project_data["service"]["queue_metadata"]["queued"])
        self.assertGreater(project_data["service"]["queue_metadata"]["due_age_seconds"], 0)
        self.assertEqual(before, state_path.read_bytes())

    def test_permission_denied_pid_check_preserves_unknown_liveness(self) -> None:
        configure(self.data_dir, capture_scope="all")
        enqueue(self.project_a, self.data_dir, clock=lambda: 1.0)
        state_path = self.data_dir / "service-state.json"
        state = json.loads(state_path.read_text())
        owner = {"pid": 12345, "nonce": "owner-token", "started_at": 1.0}
        state["owner"] = owner
        state_path.write_text(json.dumps(state))
        (self.data_dir / "service.pid").write_text(json.dumps({"version": 1, **owner}))

        with patch.object(health_check.os, "kill", side_effect=PermissionError), patch.object(health_check, "_lock_held", return_value=True):
            project = self.report()["projects"][0]

        self.assertEqual("unknown", project["service"]["worker_liveness"])
        self.assertNotEqual("stale", project["status"])
        with patch.object(health_check.os, "kill", side_effect=ProcessLookupError), patch.object(health_check, "_lock_held", return_value=True):
            project = self.report()["projects"][0]
        self.assertEqual("stale", project["service"]["worker_liveness"])
        self.assertEqual("stale", project["status"])

    def test_observer_usage_receipts_are_project_scoped_and_read_only(self) -> None:
        with Store(self.data_dir) as store:
            first = store.remember(
                self.project_a, "First raw event", "PRIVATE_OBSERVER_BODY_A",
                kind="session", source="hook:Stop", session_id="observer-a",
            )
            second = store.remember(
                self.project_b, "Second raw event", "PRIVATE_OBSERVER_BODY_B",
                kind="session", source="hook:Stop", session_id="observer-b",
            )
        self._insert_job(
            first["id"], status="running", created="2026-01-01T05:00:00Z",
            updated="2026-01-01T05:01:00Z", lease="2026-01-01T06:00:00Z",
            project=self.project_a,
        )
        self._insert_job(
            second["id"], status="running", created="2026-01-01T05:00:00Z",
            updated="2026-01-01T05:01:00Z", lease="2026-01-01T06:00:00Z",
            project=self.project_b,
        )
        first_job = f"job-running-{first['id'][:8]}"
        second_job = f"job-running-{second['id'][:8]}"
        with Store(self.data_dir) as store:
            begin_attempt(store, self.project_a, first_job, 1)
            finish_attempt(
                store, self.project_a, first_job, 1, outcome="processed",
                metrics={
                    "duration_ms": 12,
                    "usage": {
                        "status": "reported", "source": "app_server_thread_total",
                        "updates": 1,
                        "tokens": {
                            "input_tokens": 100, "cached_input_tokens": 20,
                            "cache_write_input_tokens": 10, "output_tokens": 30,
                            "reasoning_output_tokens": 5, "total_tokens": 130,
                        },
                    },
                },
            )
            begin_attempt(store, self.project_b, second_job, 1)
            finish_attempt(
                store, self.project_b, second_job, 1, outcome="processed",
                metrics={
                    "duration_ms": 99,
                    "usage": {
                        "status": "reported", "source": "app_server_thread_total",
                        "updates": 2,
                        "tokens": {
                            "input_tokens": 900, "cached_input_tokens": 300,
                            "cache_write_input_tokens": 50, "output_tokens": 100,
                            "reasoning_output_tokens": 20, "total_tokens": 1000,
                        },
                    },
                },
            )
        database = self.data_dir / "memory.sqlite3"
        before = (database.stat().st_size, database.stat().st_mtime_ns, database.read_bytes())

        first_report = self.report(self.project_a)
        second_report = self.report(self.project_b)
        first_usage = first_report["projects"][0]["observer_usage"]
        second_usage = second_report["projects"][0]["observer_usage"]

        self.assertEqual("available", first_usage["status"])
        self.assertEqual("project", first_usage["scope"])
        self.assertEqual(1, first_usage["attempts"]["recorded"])
        self.assertEqual(0, first_usage["attempts"]["without_receipt"])
        self.assertEqual(130, first_usage["totals"]["reported"]["total_tokens"])
        self.assertEqual(12, first_usage["duration_ms"]["total"])
        self.assertEqual(1000, second_usage["totals"]["reported"]["total_tokens"])
        self.assertNotIn("PRIVATE_OBSERVER", json.dumps(first_report, sort_keys=True))
        self.assertEqual(
            before,
            (database.stat().st_size, database.stat().st_mtime_ns, database.read_bytes()),
        )

    def test_missing_observer_usage_table_preserves_historical_gap_without_writes(self) -> None:
        with Store(self.data_dir) as store:
            source = store.remember(
                self.project_a, "Historical event", "PRIVATE_HISTORICAL_BODY",
                kind="session", source="hook:Stop", session_id="historical-session",
            )
        self._insert_job(
            source["id"], status="processed", created="2025-12-30T00:00:00Z",
            updated="2025-12-30T00:10:00Z", completed="2025-12-30T00:10:00Z",
            attempts=3,
        )
        database = self.data_dir / "memory.sqlite3"
        before = (database.stat().st_size, database.stat().st_mtime_ns, database.read_bytes())

        report = self.report(self.project_a)
        observer_usage = report["projects"][0]["observer_usage"]

        self.assertEqual("unavailable", observer_usage["status"])
        self.assertEqual("project", observer_usage["scope"])
        self.assertEqual(1, observer_usage["attempts"]["jobs"])
        self.assertEqual(3, observer_usage["attempts"]["expected"])
        self.assertEqual(0, observer_usage["attempts"]["recorded"])
        self.assertEqual(3, observer_usage["attempts"]["without_receipt"])
        self.assertIsNone(observer_usage["totals"]["reported"])
        self.assertIsNone(observer_usage["duration_ms"]["total"])
        self.assertNotIn("PRIVATE_HISTORICAL_BODY", json.dumps(report, sort_keys=True))
        with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'observer_usage_attempts'"
                ).fetchone()
            )
        self.assertEqual(
            before,
            (database.stat().st_size, database.stat().st_mtime_ns, database.read_bytes()),
        )

    def test_usage_ledger_totals_are_project_scoped_redacted_and_read_only(self) -> None:
        configure(self.data_dir, capture_scope="all", usage_enabled=False)
        with UsageStore(self.data_dir) as ledger:
            for thread, project, parent, quality in (
                ("root-private", self.project_a, None, "response_exact"),
                ("child-private", self.project_a, "root-private", "legacy_cumulative_delta"),
                ("foreign-private", self.project_b, None, "response_exact"),
            ):
                session = {"thread_id": thread, "session_id": parent or thread, "parent_thread_id": parent, "project": str(project)}
                event = {
                    "event_key": thread + ":event", "thread_id": thread, "session_id": parent or thread,
                    "response_id": None if parent else thread + ":response",
                    "source_kind": "legacy" if parent else "response", "quality": quality,
                    "model": "PRIVATE_MODEL_SENTINEL", "recorded_at": "2026-01-01T00:00:00Z",
                    "input_tokens": 100, "cached_input_tokens": 40, "cache_write_input_tokens": 0,
                    "output_tokens": 20, "reasoning_output_tokens": 5, "total_tokens": 120,
                }
                ledger.commit_scan(thread, None, {"offset": 100, "parser_state": {}}, session, [event])
        database = self.data_dir / "memory.sqlite3"
        before = (database.stat().st_mtime_ns, database.read_bytes())

        report = self.report()
        usage = report["projects"][0]["usage"]

        self.assertFalse(report["configuration"]["usage_enabled"])
        self.assertFalse(usage["enabled"])
        self.assertEqual("ready", usage["status"])
        self.assertEqual("unknown", usage["collection_liveness"])
        self.assertEqual(2, usage["events"])
        self.assertEqual(2, usage["threads"])
        self.assertEqual(1, usage["child_threads"])
        self.assertEqual(1, usage["root_sessions"])
        self.assertEqual(1, usage["exact_events"])
        self.assertEqual(1, usage["legacy_events"])
        self.assertEqual(0, usage["other_events"])
        self.assertEqual(240, usage["tokens"]["total_tokens"])
        self.assertEqual(200, usage["tokens"]["input_tokens"])
        self.assertEqual(80, usage["tokens"]["cached_input_tokens"])
        self.assertEqual(40, usage["tokens"]["output_tokens"])
        self.assertEqual(19200, usage["last_event_age_seconds"])
        self.assertNotIn("private", json.dumps(usage))
        self.assertNotIn("PRIVATE_MODEL_SENTINEL", json.dumps(report))
        self.assertEqual(before, (database.stat().st_mtime_ns, database.read_bytes()))
        self.assertEqual(2, len(self.report(all_projects=True)["projects"]))

    def test_optional_usage_tables_are_not_created_and_partial_ledger_is_reported(self) -> None:
        with Store(self.data_dir) as store:
            store.remember(self.project_a, "Note", "A project decision")
        report = self.report()
        self.assertEqual("missing", report["projects"][0]["usage"]["status"])
        with closing(sqlite3.connect(self.data_dir / "memory.sqlite3")) as connection, connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name = 'usage_events'").fetchone())
            connection.execute("CREATE TABLE usage_sessions (thread_id TEXT, project TEXT)")
        report = self.report()
        self.assertEqual("unavailable", report["projects"][0]["usage"]["status"])
        self.assertEqual("healthy", report["storage"]["status"])
        self.assertEqual("degraded", report["status"])
        self.assertIn("usage_metadata_unavailable", report["errors"])

    def test_usage_configuration_is_validated_without_echoing_raw_values(self) -> None:
        self.data_dir.mkdir()
        (self.data_dir / "config.json").write_text('{"usage_enabled":"PRIVATE_SENTINEL"}')
        report = self.report()
        self.assertFalse(report["configuration"]["valid"])
        self.assertIn("config_invalid", report["errors"])
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(report))

    def test_metadata_and_project_isolation_never_emit_bodies_or_foreign_records(self) -> None:
        with Store(self.data_dir) as store:
            first = store.remember(
                self.project_a,
                "Private title A",
                "PRIVATE_BODY_A",
                source="hook:Stop",
                kind="session",
            )
            second = store.remember(
                self.project_b,
                "Private title B",
                "PRIVATE_BODY_B",
                source="manual:test",
            )

        selected = self.report(self.project_a)
        rendered = json.dumps(selected, sort_keys=True)
        self.assertIn(first["id"], rendered)
        self.assertNotIn(second["id"], rendered)
        self.assertNotIn("PRIVATE_BODY_A", rendered)
        self.assertNotIn("PRIVATE_BODY_B", rendered)
        self.assertNotIn("Private title", rendered)
        self.assertEqual(1, selected["projects"][0]["records"]["entries"])

        all_projects = self.report(all_projects=True)
        self.assertEqual("all-projects", all_projects["scope"])
        self.assertEqual(2, len(all_projects["projects"]))
        self.assertEqual({str(self.project_a.resolve()), str(self.project_b.resolve())}, {item["path"] for item in all_projects["projects"]})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
