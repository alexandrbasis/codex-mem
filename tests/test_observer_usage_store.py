"""Boundaries and aggregation for observation processor metrics."""
import sqlite3
import tempfile
import unittest

from codex_mem.observer_usage_store import (
    COUNTERS,
    begin_attempt,
    finish_attempt,
    observer_usage_summary,
    recover_attempt,
    reconcile_attempts,
    snapshot_attempt,
)
from codex_mem.store import SCHEMA_VERSION, Store, project_key


class ObserverUsageStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.project = project_key("/tmp/observer-project")
        self.other_project = project_key("/tmp/other-observer-project")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def add_job(
        self,
        job_id: str,
        *,
        project: str | None = None,
        attempts: int = 1,
        model: str = "gpt-5.6-luna",
        effort: str = "medium",
    ) -> None:
        workspace = project or self.project

        def insert() -> None:
            self.store._connection.execute(
                """
                INSERT INTO observation_jobs(
                    id, project, processor_id, model, reasoning_effort, session_id,
                    input_fingerprint, input_limit, status, disposition, lease_token,
                    lease_expires_at, attempt_count, worker_thread_id, worker_turn_id,
                    error_code, output_ids_json, created_at, updated_at, completed_at
                ) VALUES (?, ?, 'observer-v1', ?, ?, 'session', ?, 1000, 'running',
                          NULL, 'lease-secret', '2099-01-01T00:00:00Z', ?, NULL, NULL,
                          NULL, '[]', '2026-09-12T00:00:00Z',
                          '2026-09-12T00:00:00Z', NULL)
                """,
                (job_id, workspace, model, effort, "f" * 63 + job_id[-1], attempts),
            )

        self.store._write(insert)

    @staticmethod
    def metrics(
        *,
        status: str = "reported",
        duration: int = 12,
        tokens: dict[str, int] | None = None,
        updates: int = 1,
    ) -> dict[str, object]:
        if tokens is None and status in {"reported", "partial"}:
            tokens = {
                "input_tokens": 100,
                "cached_input_tokens": 20,
                "cache_write_input_tokens": 10,
                "output_tokens": 30,
                "reasoning_output_tokens": 5,
                "total_tokens": 130,
            }
        return {
            "duration_ms": duration,
            "usage": {
                "status": status,
                "source": "app_server_thread_total",
                "updates": updates,
                "tokens": tokens,
            },
        }

    def test_missing_table_is_read_only_and_counts_historical_attempt_gaps(self) -> None:
        self.add_job("job-a", attempts=3)
        recover_attempt(
            self.store._connection,
            "job-a",
            3,
            outcome="failed",
            error_code="private unknown error text",
        )
        before = self.store._connection.execute("PRAGMA user_version").fetchone()[0]
        summary = observer_usage_summary(self.store._connection, self.project)

        self.assertEqual("unavailable", summary["status"])
        self.assertEqual("project", summary["scope"])
        self.assertEqual(3, summary["attempts"]["expected"])
        self.assertEqual(3, summary["attempts"]["without_receipt"])
        self.assertEqual(0, summary["attempts"]["recorded"])
        self.assertIsNone(summary["totals"]["reported"])
        self.assertIsNone(summary["duration_ms"]["total"])
        self.assertIsNone(
            self.store._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='observer_usage_attempts'"
            ).fetchone()
        )
        self.assertEqual(before, self.store._connection.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(SCHEMA_VERSION, before)

    def test_begin_requires_the_exact_positive_project_attempt(self) -> None:
        self.add_job("job-a", attempts=2)
        for project, attempt in (
            (self.other_project, 2),
            (self.project, 1),
            (self.project, 0),
        ):
            with self.subTest(project=project, attempt=attempt), self.assertRaises(ValueError):
                begin_attempt(self.store, project, "job-a", attempt)
        begin_attempt(self.store, self.project, "job-a", 2)
        begin_attempt(self.store, self.project, "job-a", 2)
        self.assertEqual(
            1,
            self.store._connection.execute(
                "SELECT COUNT(*) FROM observer_usage_attempts"
            ).fetchone()[0],
        )
        recover_attempt(
            self.store._connection,
            "job-a",
            2,
            outcome="failed",
            error_code="untrusted raw failure",
        )
        recovered = self.store._connection.execute(
            "SELECT outcome,error_code FROM observer_usage_attempts"
        ).fetchone()
        self.assertEqual(("failed", "runner_failure"), tuple(recovered))

    def test_cumulative_snapshot_is_idempotent_and_known_counters_are_immutable(self) -> None:
        self.add_job("job-a")
        begin_attempt(self.store, self.project, "job-a", 1)
        finish_attempt(
            self.store,
            self.project,
            "job-a",
            1,
            outcome="processed",
            worker_thread_id="thread-a",
            metrics=self.metrics(),
        )
        different = self.metrics(
            duration=999,
            tokens={
                "input_tokens": 9,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_output_tokens": 0,
                "total_tokens": 10,
            },
        )
        finish_attempt(
            self.store,
            self.project,
            "job-a",
            1,
            outcome="failed",
            worker_thread_id="different-thread",
            metrics=different,
        )

        row = self.store._connection.execute(
            "SELECT * FROM observer_usage_attempts"
        ).fetchone()
        self.assertEqual("processed", row["outcome"])
        self.assertEqual("thread-a", row["worker_thread_id"])
        self.assertEqual(12, row["duration_ms"])
        self.assertEqual(130, row["total_tokens"])
        summary = observer_usage_summary(self.store._connection, self.project)
        self.assertEqual(1, summary["totals"]["reported"]["attempts"])
        self.assertEqual(130, summary["totals"]["reported"]["total_tokens"])
        self.assertIsNone(summary["totals"]["partial"])

    def test_running_cumulative_snapshots_replace_before_terminal_state(self) -> None:
        self.add_job("job-a")
        begin_attempt(self.store, self.project, "job-a", 1)
        finish_attempt(
            self.store,
            self.project,
            "job-a",
            1,
            outcome="running",
            metrics=self.metrics(status="partial"),
        )
        larger = {
            "input_tokens": 200,
            "cached_input_tokens": 40,
            "cache_write_input_tokens": 10,
            "output_tokens": 60,
            "reasoning_output_tokens": 10,
            "total_tokens": 260,
        }
        finish_attempt(
            self.store,
            self.project,
            "job-a",
            1,
            outcome="processed",
            metrics=self.metrics(status="reported", duration=20, tokens=larger, updates=2),
        )
        row = self.store._connection.execute(
            "SELECT outcome,usage_status,usage_updates,total_tokens FROM observer_usage_attempts"
        ).fetchone()
        self.assertEqual(("processed", "reported", 2, 260), tuple(row))

    def test_invalid_final_snapshot_clears_partial_counters_to_unknown(self) -> None:
        self.add_job("job-a")
        begin_attempt(self.store, self.project, "job-a", 1)
        finish_attempt(
            self.store,
            self.project,
            "job-a",
            1,
            outcome="running",
            metrics=self.metrics(status="partial"),
        )
        finish_attempt(
            self.store,
            self.project,
            "job-a",
            1,
            outcome="failed",
            error_code="invalid_response",
            metrics=self.metrics(status="invalid", tokens=None, updates=2),
        )
        row = self.store._connection.execute(
            "SELECT usage_status,total_tokens FROM observer_usage_attempts"
        ).fetchone()
        self.assertEqual(("invalid", None), tuple(row))
        summary = observer_usage_summary(self.store._connection, self.project)
        self.assertEqual(1, summary["attempts"]["unknown"])
        self.assertIsNone(summary["totals"]["partial"])

    def test_unavailable_final_receipt_preserves_durable_partial_snapshot(self) -> None:
        self.add_job("job-a")
        begin_attempt(self.store, self.project, "job-a", 1)
        snapshot_attempt(self.store, self.project, "job-a", 1,
                         worker_thread_id="worker-a", worker_turn_id="turn-a",
                         metrics=self.metrics(status="partial", updates=3))
        finish_attempt(self.store, self.project, "job-a", 1, outcome="failed",
                       error_code="timeout",
                       metrics=self.metrics(status="unavailable", updates=0))
        row = self.store._connection.execute("SELECT * FROM observer_usage_attempts").fetchone()
        self.assertEqual(("failed", "partial", 130, 3, "worker-a", "turn-a"),
                         tuple(row[k] for k in ("outcome", "usage_status", "total_tokens",
                                                "usage_updates", "worker_thread_id", "worker_turn_id")))

    def test_crash_recovery_keeps_checkpoint_and_monotonic_late_final(self) -> None:
        self.add_job("job-a")
        begin_attempt(self.store, self.project, "job-a", 1)
        snapshot_attempt(self.store, self.project, "job-a", 1,
                         worker_thread_id="worker-a", metrics=self.metrics(status="partial"))
        self.store.close()
        self.store = Store(self.tmp.name)
        self.store._write(lambda: recover_attempt(self.store._connection, "job-a", 1,
                                                  outcome="lease_expired"))
        row = self.store._connection.execute("SELECT * FROM observer_usage_attempts").fetchone()
        self.assertEqual(("lease_expired", "partial", 130),
                         tuple(row[k] for k in ("outcome", "usage_status", "total_tokens")))
        larger = self.metrics(status="reported")
        larger["usage"]["tokens"] = {k: v * 2 for k, v in larger["usage"]["tokens"].items()}
        # Reclaimed attempts ignore any late intermediate callbacks.
        snapshot_attempt(self.store, self.project, "job-a", 1,
                         worker_thread_id="worker-a", metrics=larger)
        self.assertEqual(130, observer_usage_summary(self.store._connection, self.project)
                         ["totals"]["partial"]["total_tokens"])
        # A final receipt may complete the same worker's partial accounting,
        # but cannot change its recovered operational outcome.
        finish_attempt(self.store, self.project, "job-a", 1, outcome="processed",
                       worker_thread_id="worker-a", metrics=larger)
        row = self.store._connection.execute("SELECT * FROM observer_usage_attempts").fetchone()
        self.assertEqual(("lease_expired", "reported", 260),
                         tuple(row[k] for k in ("outcome", "usage_status", "total_tokens")))

    def test_checkpoint_regression_is_not_summed_or_reported_as_complete(self) -> None:
        self.add_job("job-a")
        begin_attempt(self.store, self.project, "job-a", 1)
        snapshot_attempt(self.store, self.project, "job-a", 1, metrics=self.metrics(status="partial"))
        smaller = self.metrics()
        smaller["usage"]["tokens"] = {k: 0 for k in COUNTERS}
        finish_attempt(self.store, self.project, "job-a", 1, outcome="processed", metrics=smaller)
        row = self.store._connection.execute("SELECT usage_status,total_tokens FROM observer_usage_attempts").fetchone()
        self.assertEqual(("invalid", None), tuple(row))

    def test_recovery_without_a_checkpoint_explicitly_marks_usage_unavailable(self) -> None:
        self.add_job("job-a")
        begin_attempt(self.store, self.project, "job-a", 1)
        self.store._write(lambda: recover_attempt(self.store._connection, "job-a", 1,
                                                  outcome="lease_expired"))
        row = self.store._connection.execute("SELECT usage_status,total_tokens FROM observer_usage_attempts").fetchone()
        self.assertEqual(("unavailable", None), tuple(row))

    def test_restart_reconciliation_is_bounded_scoped_and_preserves_partial_evidence(self) -> None:
        self.add_job("job-a")
        self.add_job("job-b")
        self.add_job("job-c", project=self.other_project)
        self.add_job("job-d")
        for job_id, workspace in (("job-a", self.project), ("job-b", self.project),
                                  ("job-c", self.other_project), ("job-d", self.project)):
            begin_attempt(self.store, workspace, job_id, 1)
            snapshot_attempt(self.store, workspace, job_id, 1, metrics=self.metrics(status="partial"))
        self.store._write(lambda: self.store._connection.execute(
            "UPDATE observation_jobs SET status='skipped' WHERE id IN ('job-a','job-c')"))
        self.store._write(lambda: self.store._connection.execute(
            "UPDATE observation_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id='job-b'"))
        self.assertEqual(1, reconcile_attempts(self.store, self.project, limit=1))
        self.assertEqual(1, reconcile_attempts(self.store, self.project, limit=1))
        self.assertEqual(0, reconcile_attempts(self.store, self.project))
        rows = {row["job_id"]: row for row in self.store._connection.execute(
            "SELECT * FROM observer_usage_attempts")}
        self.assertEqual("skipped", rows["job-a"]["outcome"])
        self.assertEqual("lease_expired", rows["job-b"]["outcome"])
        self.assertEqual("running", rows["job-c"]["outcome"])
        self.assertEqual("running", rows["job-d"]["outcome"])
        self.assertTrue(all(row["usage_status"] == "partial" and row["total_tokens"] == 130
                            for row in rows.values()))
        self.assertEqual(1, reconcile_attempts(self.store))
        self.assertEqual(0, reconcile_attempts(self.store))

    def test_duplicate_terminal_finish_can_enrich_null_metrics(self) -> None:
        self.add_job("job-a")
        begin_attempt(self.store, self.project, "job-a", 1)
        finish_attempt(self.store, self.project, "job-a", 1, outcome="failed")
        finish_attempt(
            self.store,
            self.project,
            "job-a",
            1,
            outcome="processed",
            error_code="timeout",
            worker_turn_id="turn-a",
            metrics=self.metrics(status="partial"),
        )
        row = self.store._connection.execute(
            "SELECT * FROM observer_usage_attempts"
        ).fetchone()
        self.assertEqual("failed", row["outcome"])
        self.assertEqual("timeout", row["error_code"])
        self.assertEqual("turn-a", row["worker_turn_id"])
        self.assertEqual("partial", row["usage_status"])
        self.assertEqual(130, row["total_tokens"])

    def test_retries_are_separate_and_gaps_are_not_hidden(self) -> None:
        self.add_job("job-a")
        begin_attempt(self.store, self.project, "job-a", 1)
        finish_attempt(
            self.store,
            self.project,
            "job-a",
            1,
            outcome="lease_expired",
            error_code="lease_expired",
            metrics=self.metrics(status="unavailable"),
        )
        self.store._write(
            lambda: self.store._connection.execute(
                "UPDATE observation_jobs SET attempt_count=3 WHERE id='job-a'"
            )
        )
        begin_attempt(self.store, self.project, "job-a", 3)
        finish_attempt(
            self.store,
            self.project,
            "job-a",
            3,
            outcome="processed",
            metrics=self.metrics(status="partial"),
        )
        summary = observer_usage_summary(self.store._connection, self.project)
        self.assertEqual(3, summary["attempts"]["expected"])
        self.assertEqual(2, summary["attempts"]["recorded"])
        self.assertEqual(1, summary["attempts"]["without_receipt"])
        self.assertEqual(1, summary["attempts"]["partial"])
        self.assertEqual(1, summary["attempts"]["unknown"])
        self.assertEqual(1, summary["attempts"]["outcomes"]["lease_expired"])

    def test_late_old_attempt_records_cost_without_changing_current_attempt(self) -> None:
        self.add_job("job-a")
        begin_attempt(self.store, self.project, "job-a", 1)
        def reclaim() -> None:
            recover_attempt(
                self.store._connection,
                "job-a",
                1,
                outcome="lease_expired",
            )
            self.store._connection.execute(
                "UPDATE observation_jobs SET attempt_count=2 WHERE id='job-a'"
            )

        self.store._write(reclaim)
        begin_attempt(self.store, self.project, "job-a", 2)
        expired = self.store._connection.execute(
            "SELECT outcome,error_code,total_tokens FROM observer_usage_attempts "
            "WHERE job_id='job-a' AND attempt_count=1"
        ).fetchone()
        self.assertEqual(("lease_expired", "lease_expired", None), tuple(expired))

        finish_attempt(
            self.store,
            self.project,
            "job-a",
            1,
            outcome="processed",
            metrics=self.metrics(status="partial"),
        )
        current = self.store._connection.execute(
            "SELECT outcome,total_tokens FROM observer_usage_attempts "
            "WHERE job_id='job-a' AND attempt_count=2"
        ).fetchone()
        self.assertEqual(("running", None), tuple(current))

        finish_attempt(
            self.store,
            self.project,
            "job-a",
            2,
            outcome="processed",
            metrics=self.metrics(),
        )
        summary = observer_usage_summary(self.store._connection, self.project)
        self.assertEqual(2, summary["attempts"]["recorded"])
        self.assertEqual(1, summary["attempts"]["partial"])
        self.assertEqual(1, summary["attempts"]["reported"])
        self.assertEqual(1, summary["attempts"]["outcomes"]["lease_expired"])
        self.assertEqual(1, summary["attempts"]["outcomes"]["processed"])

    def test_store_lease_reclaim_expires_prior_running_receipt(self) -> None:
        self.store.remember(
            self.project,
            "source",
            "Fictional reclaim source.",
            source="hook:PostToolUse",
            session_id="reclaim-session",
        )
        first = self.store.claim_observation_batch(
            self.project,
            "codex-mem-native-observation-v1",
            "gpt-5.6-luna",
            "medium",
            lease_seconds=1,
        )
        self.assertIsNotNone(first)
        begin_attempt(self.store, self.project, first["job_id"], first["attempt_count"])
        self.store._write(
            lambda: self.store._connection.execute(
                "UPDATE observation_jobs SET lease_expires_at='2000-01-01T00:00:00Z' "
                "WHERE id=?",
                (first["job_id"],),
            )
        )
        reclaimed = self.store.claim_observation_batch(
            self.project,
            "codex-mem-native-observation-v1",
            "gpt-5.6-luna",
            "medium",
            lease_seconds=1,
        )
        self.assertEqual(2, reclaimed["attempt_count"])
        begin_attempt(
            self.store,
            self.project,
            reclaimed["job_id"],
            reclaimed["attempt_count"],
        )
        rows = self.store._connection.execute(
            "SELECT attempt_count,outcome,error_code FROM observer_usage_attempts "
            "ORDER BY attempt_count"
        ).fetchall()
        self.assertEqual(
            [(1, "lease_expired", "lease_expired"), (2, "running", None)],
            [tuple(row) for row in rows],
        )

    def test_failed_claim_recovery_preserves_known_failure_code(self) -> None:
        self.store.remember(
            self.project,
            "source",
            "Fictional failed retry source.",
            source="hook:PostToolUse",
            session_id="failed-retry-session",
        )
        first = self.store.claim_observation_batch(
            self.project,
            "codex-mem-native-observation-v1",
            "gpt-5.6-luna",
            "medium",
        )
        begin_attempt(self.store, self.project, first["job_id"], first["attempt_count"])
        self.store.fail_observation_batch(
            self.project,
            first["job_id"],
            first["lease_token"],
            code="timeout",
        )

        retried = self.store.claim_observation_batch(
            self.project,
            "codex-mem-native-observation-v1",
            "gpt-5.6-luna",
            "medium",
            retry_failed=True,
        )
        begin_attempt(self.store, self.project, retried["job_id"], retried["attempt_count"])
        rows = self.store._connection.execute(
            "SELECT attempt_count,outcome,error_code FROM observer_usage_attempts "
            "ORDER BY attempt_count"
        ).fetchall()
        self.assertEqual(
            [(1, "failed", "timeout"), (2, "running", None)],
            [tuple(row) for row in rows],
        )

    def test_project_and_model_groups_do_not_cross_contaminate(self) -> None:
        self.add_job("job-a", model="model-a")
        self.add_job("job-b", project=self.other_project, model="model-b")
        for project, job in ((self.project, "job-a"), (self.other_project, "job-b")):
            begin_attempt(self.store, project, job, 1)
            finish_attempt(
                self.store,
                project,
                job,
                1,
                outcome="processed",
                metrics=self.metrics(),
            )
        first = observer_usage_summary(self.store._connection, self.project)
        second = observer_usage_summary(self.store._connection, self.other_project)
        self.assertEqual(["model-a"], [group["model"] for group in first["models"]])
        self.assertEqual("requested_job_profile", first["models"][0]["model_basis"])
        self.assertEqual(["model-b"], [group["model"] for group in second["models"]])
        self.assertEqual(130, first["totals"]["reported"]["total_tokens"])
        self.assertEqual(130, second["totals"]["reported"]["total_tokens"])

    def test_invalid_counters_do_not_change_the_running_receipt(self) -> None:
        self.add_job("job-a")
        begin_attempt(self.store, self.project, "job-a", 1)
        invalid = self.metrics()
        invalid["usage"]["tokens"]["cached_input_tokens"] = 101
        with self.assertRaises(ValueError):
            finish_attempt(
                self.store,
                self.project,
                "job-a",
                1,
                outcome="processed",
                metrics=invalid,
            )
        row = self.store._connection.execute(
            "SELECT * FROM observer_usage_attempts"
        ).fetchone()
        self.assertEqual("running", row["outcome"])
        for counter in COUNTERS:
            self.assertIsNone(row[counter])
        with self.assertRaises(ValueError):
            finish_attempt(
                self.store,
                self.project,
                "job-a",
                1,
                outcome="failed",
                error_code="raw exception text must not be stored",
            )

    def test_read_only_connection_and_source_content_preservation(self) -> None:
        self.add_job("job-a")
        secret = "source prompt and model output must remain outside the usage ledger"
        entry_id = self.store.remember(
            self.project,
            "source",
            secret,
            source="hook:UserPromptSubmit",
            session_id="session",
        )["id"]
        self.store._write(
            lambda: self.store._connection.execute(
                "INSERT INTO observation_job_sources(job_id,source_id) VALUES ('job-a',?)",
                (entry_id,),
            )
        )
        begin_attempt(self.store, self.project, "job-a", 1)
        finish_attempt(
            self.store,
            self.project,
            "job-a",
            1,
            outcome="processed",
            metrics=self.metrics(),
        )
        stored_source = self.store._connection.execute(
            "SELECT body FROM entries WHERE id=?", (entry_id,)
        ).fetchone()[0]
        ledger_text = " ".join(
            str(value)
            for value in self.store._connection.execute(
                "SELECT * FROM observer_usage_attempts"
            ).fetchone()
            if value is not None
        )
        self.assertEqual(secret, stored_source)
        self.assertNotIn(secret, ledger_text)

        read_only = sqlite3.connect(f"file:{self.store.db_path}?mode=ro", uri=True)
        try:
            summary = observer_usage_summary(read_only, self.project)
        finally:
            read_only.close()
        self.assertEqual(1, summary["attempts"]["recorded"])
        self.assertEqual(130, summary["totals"]["reported"]["total_tokens"])

        self.store._write(
            lambda: self.store._connection.execute(
                "DELETE FROM observation_jobs WHERE id='job-a'"
            )
        )
        self.assertEqual(
            0,
            self.store._connection.execute(
                "SELECT COUNT(*) FROM observer_usage_attempts"
            ).fetchone()[0],
        )
        self.assertEqual(
            secret,
            self.store._connection.execute(
                "SELECT body FROM entries WHERE id=?", (entry_id,)
            ).fetchone()[0],
        )


if __name__ == "__main__":
    unittest.main()
