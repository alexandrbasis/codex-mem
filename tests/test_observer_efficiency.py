"""Outcome accounting measures recorded overhead without claiming usefulness."""
import json
import sqlite3
import tempfile
import unittest

from codex_mem.observer_usage_store import (
    COUNTERS,
    OUTCOMES,
    begin_attempt,
    finish_attempt,
    observer_usage_summary,
)
from codex_mem.store import Store, project_key


class ObserverEfficiencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.project = project_key("/tmp/efficiency-project")
        self.other = project_key("/tmp/efficiency-other")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def job(self, job_id, *, attempts=1, status="running", outputs=(), project=None):
        self.store._write(lambda: self.store._connection.execute(
            """INSERT INTO observation_jobs(
                id, project, processor_id, model, reasoning_effort,
                input_fingerprint, input_limit, status, attempt_count,
                output_ids_json, created_at, updated_at
            ) VALUES (?, ?, 'observer-v1', 'gpt-5.6-luna', 'medium', ?, 1000,
                      ?, ?, ?, '2026-09-17T00:00:00Z', '2026-09-17T00:00:00Z')""",
            (job_id, project or self.project, job_id, status, attempts,
             json.dumps(outputs)),
        ))

    def note(self, *, project=None, kind="note", source="processor:observer-v1"):
        return self.store.remember(
            project or self.project, "fictional output", "fixture only",
            kind=kind, source=source,
        )["id"]

    def attempt(self, job_id, *, number=1, outcome="processed", status="reported",
                tokens=100, duration=20, project=None):
        workspace = project or self.project
        begin_attempt(self.store, workspace, job_id, number)
        metrics = None
        if duration is not None:
            counters = {counter: 0 for counter in COUNTERS}
            counters.update(input_tokens=tokens, total_tokens=tokens)
            metrics = {"duration_ms": duration, "usage": {
                "status": status, "source": "app_server_thread_total", "updates": 1,
                "tokens": counters if status in {"reported", "partial"} else None,
            }}
        finish_attempt(self.store, workspace, job_id, number,
                       outcome=outcome, metrics=metrics)

    def summary(self):
        return observer_usage_summary(self.store._connection, self.project)

    def test_outcomes_use_recorded_receipts_and_keep_historical_gaps(self):
        self.job("old", attempts=3)
        for index, outcome in enumerate(OUTCOMES):
            self.job(outcome)
            self.attempt(outcome, outcome=outcome,
                         status="partial" if outcome == "running" else "reported",
                         tokens=100 * (index + 1), duration=10 * (index + 1))

        efficiency = self.summary()["efficiency"]
        self.assertEqual("not_measured", efficiency["net_savings"])
        self.assertEqual("not_measured", efficiency["output_quality"])
        coverage = efficiency["coverage"]
        self.assertEqual(8, coverage["expected_attempts"])
        self.assertEqual(5, coverage["recorded_attempts"])
        self.assertEqual(3, coverage["attempts_without_receipt"])
        self.assertEqual(62.5, coverage["receipt_percent_of_expected_attempts"])
        self.assertEqual(80.0, coverage["reported_percent_of_recorded_attempts"])
        self.assertEqual(50.0, coverage["reported_percent_of_expected_attempts"])
        for index, outcome in enumerate(OUTCOMES):
            item = efficiency["outcomes"][outcome]
            status = "partial" if outcome == "running" else "reported"
            self.assertEqual(1, item["attempts"])
            self.assertEqual(20.0, item["percent_of_recorded_attempts"])
            self.assertEqual(100 * (index + 1), item["totals"][status]["total_tokens"])
            self.assertEqual(10 * (index + 1), item["duration_ms"]["average"])

    def test_partial_counter_columns_are_null_and_have_observed_denominators(self):
        self.job("partial")
        self.attempt("partial", outcome="failed", status="partial")
        # Older or interrupted receipt data may have only some counters.
        self.store._write(lambda: self.store._connection.execute(
            "UPDATE observer_usage_attempts SET total_tokens=NULL, output_tokens=NULL "
            "WHERE job_id='partial'"))
        self.job("unknown")
        self.attempt("unknown", outcome="failed", duration=None)
        summary = self.summary()
        self.assertIsNone(summary["totals"]["partial"]["total_tokens"])
        item = summary["efficiency"]["outcomes"]["failed"]
        self.assertEqual(2, item["attempts"])
        self.assertEqual(1, item["usage_status"]["unknown"])
        self.assertEqual(1, item["duration_ms"]["count"])
        partial = item["totals"]["partial"]
        self.assertIsNone(partial["total_tokens"])
        self.assertEqual(0, partial["counter_attempts"]["total_tokens"])
        self.assertEqual(100, partial["input_tokens"])
        self.assertEqual(1, partial["counter_attempts"]["input_tokens"])
        self.assertIsNone(item["totals"]["reported"])

    def test_output_cost_includes_retries_once_and_counts_distinct_outputs(self):
        note = self.note()
        summary_note = self.note(kind="session_summary")
        self.job("retried", status="processed", outputs=[note, note, summary_note])
        self.attempt("retried", outcome="failed", tokens=40, duration=10)
        self.store._write(lambda: self.store._connection.execute(
            "UPDATE observation_jobs SET attempt_count=2 WHERE id='retried'"))
        self.attempt("retried", number=2, tokens=160, duration=30)

        efficiency = self.summary()["efficiency"]
        outputs = efficiency["outputs"]
        self.assertEqual(1, outputs["processed_jobs"])
        self.assertEqual(2, outputs["persisted_output_records"])
        self.assertEqual(1, outputs["notes"])
        self.assertEqual(1, outputs["session_summaries"])
        cost = efficiency["per_output"]["tokens"]
        self.assertEqual(1, cost["jobs"])
        self.assertEqual(2, cost["attempts"])
        self.assertEqual(200, cost["total_tokens"])
        self.assertEqual(2, cost["output_records"])
        self.assertEqual(100.0, cost["total_tokens_per_output_record"])
        duration = efficiency["per_output"]["duration_ms"]
        self.assertEqual(40, duration["total"])
        self.assertEqual(20.0, duration["per_output_record"])

    def test_outputs_require_processed_job_same_project_and_processor_provenance(self):
        own = self.note()
        foreign = self.note(project=self.other)
        raw = self.note(source="hook:PostToolUse")
        wrong_processor = self.note(source="processor:another-observer")
        excluded = self.note()
        self.job("success", status="processed", outputs=[own, foreign, raw, wrong_processor, "missing"])
        self.attempt("success")
        self.job("failed", status="failed", outputs=[excluded])
        self.attempt("failed", outcome="failed")
        self.job("foreign", status="processed", outputs=[foreign], project=self.other)
        self.attempt("foreign", project=self.other, tokens=9000)
        efficiency = self.summary()["efficiency"]
        self.assertEqual(1, efficiency["outputs"]["notes"])
        self.assertEqual(4, efficiency["outputs"]["unresolved_output_references"])
        self.assertEqual(1, efficiency["outputs"]["jobs_with_incomplete_output_metadata"])
        self.assertEqual(1, efficiency["outcomes"]["processed"]["attempts"])
        self.assertEqual(0, efficiency["per_output"]["tokens"]["jobs"])
        self.assertIsNone(efficiency["per_output"]["tokens"]["total_tokens_per_output_record"])

    def test_missing_retry_receipts_and_partial_costs_do_not_get_per_output_ratios(self):
        for job_id in ("gap", "partial", "wrong-final"):
            self.job(job_id, attempts=2, status="processed", outputs=[self.note()])
            self.attempt(job_id, number=2,
                         outcome="failed" if job_id == "wrong-final" else "processed",
                         status="partial" if job_id == "partial" else "reported")
        efficiency = self.summary()["efficiency"]
        self.assertEqual(3, efficiency["outputs"]["notes"])
        self.assertEqual(1, efficiency["outputs"]["jobs_without_success_receipt"])
        self.assertEqual(0, efficiency["per_output"]["tokens"]["jobs"])
        self.assertEqual(3, efficiency["per_output"]["tokens"]["excluded_processed_jobs"])
        self.assertIsNone(efficiency["per_output"]["tokens"]["total_tokens"])
        self.assertIsNone(efficiency["per_output"]["duration_ms"]["total"])

    def test_each_per_output_metric_uses_its_own_complete_job_subset(self):
        for job_id in ("reported", "partial"):
            self.job(job_id, status="processed", outputs=[self.note()])
            self.attempt(job_id, status=job_id, tokens=100, duration=20)
        efficiency = self.summary()["efficiency"]
        tokens = efficiency["per_output"]["tokens"]
        self.assertEqual(1, tokens["jobs"])
        self.assertEqual(1, tokens["excluded_processed_jobs"])
        self.assertEqual(1, tokens["output_records"])
        self.assertEqual(100.0, tokens["total_tokens_per_output_record"])
        durations = efficiency["per_output"]["duration_ms"]
        self.assertEqual(2, durations["jobs"])
        self.assertEqual(2, durations["output_records"])
        self.assertEqual(20.0, durations["per_output_record"])

    def test_unfinalized_retry_snapshot_is_not_complete_per_output_duration(self):
        self.job("stale", outputs=[self.note()])
        self.attempt("stale", outcome="running", status="partial", duration=10)
        self.store._write(lambda: self.store._connection.execute(
            "UPDATE observation_jobs SET attempt_count=2, status='processed' WHERE id='stale'"))
        self.attempt("stale", number=2, duration=30)
        duration = self.summary()["efficiency"]["per_output"]["duration_ms"]
        self.assertEqual(0, duration["jobs"])
        self.assertIsNone(duration["per_output_record"])

    def test_absent_optional_table_still_reports_outputs_without_writes(self):
        self.job("old", attempts=3, status="processed", outputs=[self.note()])
        before = self.store._connection.total_changes
        read_only = sqlite3.connect(f"file:{self.store.db_path}?mode=ro", uri=True)
        try:
            efficiency = observer_usage_summary(read_only, self.project)["efficiency"]
        finally:
            read_only.close()
        self.assertEqual(before, self.store._connection.total_changes)
        self.assertIsNone(self.store._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='observer_usage_attempts'"
        ).fetchone())
        self.assertEqual(1, efficiency["outputs"]["notes"])
        self.assertEqual(1, efficiency["outputs"]["jobs_without_success_receipt"])
        self.assertEqual(3, efficiency["coverage"]["attempts_without_receipt"])
        self.assertEqual(0.0, efficiency["coverage"]["receipt_percent_of_expected_attempts"])
        self.assertIsNone(efficiency["coverage"]["reported_percent_of_recorded_attempts"])
        self.assertIsNone(efficiency["per_output"]["tokens"]["total_tokens"])

    def test_empty_project_and_malformed_manifests_remain_read_only(self):
        empty = self.summary()["efficiency"]
        self.assertIsNone(empty["coverage"]["receipt_percent_of_expected_attempts"])
        self.assertIsNone(empty["per_output"]["tokens"]["total_tokens_per_output_record"])
        for job_id, manifest in (("invalid", "{bad"), ("object", '{"id":"note"}')):
            self.job(job_id, status="processed")
            self.attempt(job_id)
            self.store._write(lambda: self.store._connection.execute(
                "UPDATE observation_jobs SET output_ids_json=? WHERE id=?", (manifest, job_id)))
        before = self.store._connection.total_changes
        read_only = sqlite3.connect(f"file:{self.store.db_path}?mode=ro", uri=True)
        try:
            efficiency = observer_usage_summary(read_only, self.project)["efficiency"]
        finally:
            read_only.close()
        self.assertEqual(before, self.store._connection.total_changes)
        self.assertEqual(2, efficiency["outputs"]["jobs_with_incomplete_output_metadata"])
        self.assertEqual(0, efficiency["outputs"]["persisted_output_records"])
        self.assertIsNone(efficiency["per_output"]["tokens"]["total_tokens"])


if __name__ == "__main__":
    unittest.main()
