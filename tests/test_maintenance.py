from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_mem.config import configure
from codex_mem.service import enqueue
from codex_mem.store import Store, project_key


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
            now=1_767_244_800.0,  # 2026-01-02T00:00:00Z
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

    def _insert_job(self, entry_id: str, *, status: str, created: str, updated: str, lease: str | None = None) -> None:
        database = self.data_dir / "memory.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                INSERT INTO observation_jobs(
                    id, project, processor_id, model, reasoning_effort, session_id,
                    input_fingerprint, input_limit, status, disposition, lease_token,
                    lease_expires_at, attempt_count, worker_thread_id, worker_turn_id,
                    error_code, output_ids_json, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 1, NULL, NULL, ?, '[]', ?, ?, NULL)
                """,
                (
                    f"job-{status}-{entry_id[:8]}",
                    project_key(self.project_a),
                    "codex-mem-native-observation-v1",
                    "gpt-5.6-luna",
                    "medium",
                    "session-1",
                    ("f" if status == "failed" else "e") * 64,
                    12000,
                    status,
                    "lease-token" if lease else None,
                    lease,
                    "failed-code" if status == "failed" else None,
                    created,
                    updated,
                ),
            )
            connection.execute(
                "INSERT INTO observation_job_sources(job_id, source_id) VALUES (?, ?)",
                (f"job-{status}-{entry_id[:8]}", entry_id),
            )

    def test_idle_project_is_distinct_from_blocked_and_stale_queue(self) -> None:
        with Store(self.data_dir) as store:
            blocked = store.remember(
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
            blocked["id"],
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

        blocked_report = self.report(self.project_a)
        project_data = blocked_report["projects"][0]
        self.assertEqual("blocked", project_data["status"])
        self.assertEqual(1, project_data["observation_queue"]["counts"]["failed"])
        self.assertEqual(1, project_data["observation_queue"]["stale_running"])
        self.assertEqual("gpt-5.6-luna", project_data["processing"]["model_profiles"][0]["model"])
        self.assertEqual("medium", project_data["processing"]["model_profiles"][0]["reasoning_effort"])

        with Store(self.data_dir) as store:
            store.remember(self.project_b, "Idle note", "separate project body", source="manual:test")
        idle_report = self.report(self.project_b)
        self.assertEqual("healthy", idle_report["projects"][0]["status"])
        self.assertEqual("idle", idle_report["projects"][0]["observation_queue"]["status"])

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
