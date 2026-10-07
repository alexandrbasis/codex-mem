"""Transport recovery requires an unchanged failure before model dispatch."""

from pathlib import Path
import tempfile
import unittest

from codex_mem.processor import MODEL, PROCESSOR_ID, REASONING_EFFORT
from codex_mem.store import Store


class StoreTransportRetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = Store(self.root / "memory")
        self.addCleanup(self.store.close)

    def fail_job(self, code, *, thread_id=None, turn_id=None, owner=None):
        owner = self.project if owner is None else owner
        raw = self.store.remember(owner, code, "original evidence",
                                  source="hook:PostToolUse", session_id=code)
        job = self.store.claim_observation_batch(
            owner, PROCESSOR_ID, MODEL, REASONING_EFFORT,
            worker_thread_id=thread_id, worker_turn_id=turn_id,
        )
        self.store.fail_observation_batch(owner, job["job_id"], job["lease_token"], code)
        return raw, job

    def retry(self, job, code, attempt=1, **kwargs):
        return self.store.claim_observation_batch(
            self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
            retry_job_id=job["job_id"], retry_error_code=code,
            retry_attempt_count=attempt, **kwargs,
        )

    def row(self, job):
        return dict(self.store._connection.execute(
            "SELECT * FROM observation_jobs WHERE id=?", (job["job_id"],)
        ).fetchone())

    def test_guarded_transport_retry_preserves_snapshot_and_takes_new_lease(self):
        for code in ("runner_unavailable", "protocol_error"):
            with self.subTest(code=code):
                raw, job = self.fail_job(code)
                before = self.row(job)
                retried = self.retry(job, code)
                after = self.row(job)
                self.assertEqual(job["job_id"], retried["job_id"])
                self.assertEqual([raw["id"]], [item["id"] for item in retried["sources"]])
                self.assertEqual("original evidence", retried["sources"][0]["body"])
                self.assertEqual(2, retried["attempt_count"])
                self.assertNotEqual(job["lease_token"], retried["lease_token"])
                self.assertEqual(before["input_fingerprint"], after["input_fingerprint"])
                self.assertIsNone(after["worker_thread_id"])
                self.assertIsNone(after["worker_turn_id"])
                self.assertEqual("running", after["status"])
                self.store.fail_observation_batch(
                    self.project, retried["job_id"], retried["lease_token"], code)

    def test_changed_snapshot_refuses_without_fresh_work_fallback(self):
        changes = (
            ("attempt_count", 2), ("error_code", "runner_failure"),
            ("status", "running"), ("status", "processed"), ("status", "skipped"),
            ("worker_thread_id", "known-model-thread"), ("worker_thread_id", ""),
            ("worker_turn_id", "known-model-turn"), ("worker_turn_id", ""),
            ("lease_token", "unexpected-lease"), ("lease_expires_at", "2999-01-01T00:00:00Z"),
            ("output_ids_json", '["recorded-output"]'),
        )
        for code in ("runner_unavailable", "protocol_error"):
            for column, value in changes:
                with self.subTest(code=code, column=column, value=value):
                    raw, job = self.fail_job(code)
                    self.store._connection.execute(
                        f"UPDATE observation_jobs SET {column}=? WHERE id=?",
                        (value, job["job_id"]),
                    )
                    fresh = self.store.remember(
                        self.project, "fresh", "unrelated evidence", source="hook:PostToolUse",
                        session_id="fresh",
                    )
                    before = self.row(job)
                    self.assertIsNone(self.retry(job, code))
                    self.assertEqual(before, self.row(job))
                    self.assertIsNone(self.store.get(self.project, [raw["id"]])[0]["superseded_by"])
                    # Retire this fixture's sources so the next subtest can make its own job.
                    self.store._connection.execute(
                        "UPDATE entries SET superseded_by=? WHERE id IN (?,?)",
                        (raw["id"], raw["id"], fresh["id"]),
                    )
                    self.store._connection.execute(
                        "UPDATE observation_jobs SET status='failed', lease_token=NULL, "
                        "lease_expires_at=NULL WHERE id=?", (job["job_id"],),
                    )

    def test_known_turn_is_refused_when_recorded_at_failure(self):
        _, job = self.fail_job("protocol_error", turn_id="dispatched-turn")
        before = self.row(job)
        self.assertIsNone(self.retry(job, "protocol_error"))
        self.assertEqual(before, self.row(job))

    def test_exact_retry_refuses_superseded_source(self):
        raw, job = self.fail_job("runner_unavailable")
        replacement = self.store.remember(self.project, "replacement", "new evidence")
        self.store._connection.execute("UPDATE entries SET superseded_by=? WHERE id=?",
                                       (replacement["id"], raw["id"]))
        before = self.row(job)
        self.assertIsNone(self.retry(job, "runner_unavailable"))
        self.assertEqual(before, self.row(job))

    def test_existing_guarded_retry_policy_is_unchanged_for_known_turn(self):
        _, job = self.fail_job("timeout", thread_id="worker-thread", turn_id="dispatched-turn")
        retried = self.retry(job, "timeout")
        self.assertEqual(job["job_id"], retried["job_id"])
        self.assertEqual(2, retried["attempt_count"])

    def test_exact_transport_retry_respects_active_project_lease(self):
        _, job = self.fail_job("runner_unavailable")
        self.store.remember(self.project, "active", "active evidence", source="hook:PostToolUse")
        active = self.store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
        self.assertIsNotNone(active)
        before = self.row(job)
        self.assertIsNone(self.retry(job, "runner_unavailable"))
        self.assertEqual(before, self.row(job))

    def test_transport_code_requires_guarded_selector(self):
        _, job = self.fail_job("protocol_error")
        self.assertIsNone(self.store.claim_observation_batch(
            self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT, retry_job_id=job["job_id"]))
        for kwargs in ({"retry_error_code": "protocol_error"},
                       {"retry_attempt_count": 1},
                       {"retry_error_code": "protocol_error", "retry_attempt_count": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.claim_observation_batch(
                    self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                    retry_job_id=job["job_id"], **kwargs)

    def test_exact_retry_refuses_foreign_and_missing_jobs(self):
        foreign = self.root / "foreign"
        foreign.mkdir()
        _, job = self.fail_job("runner_unavailable", owner=foreign)
        before = self.row(job)
        self.assertIsNone(self.retry(job, "runner_unavailable"))
        self.assertEqual(before, self.row(job))
        self.assertIsNone(self.retry({"job_id": "f" * 32}, "runner_unavailable"))


if __name__ == "__main__":
    unittest.main()
