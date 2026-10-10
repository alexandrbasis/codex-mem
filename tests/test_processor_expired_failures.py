"""Operational errors after lease expiry retain the bounded recovery path."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem.jev_filter import JevFilterError
from codex_mem.processor import MODEL, REASONING_EFFORT, ProcessorFailure, process_pending
from codex_mem.store import Store, StoreError


class ProcessorExpiredFailureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name) / "project"
        self.project.mkdir()
        self.home = Path(self.temporary.name) / "memory"
        with Store(self.home) as store:
            self.raw = store.remember(self.project, "Event", "Keep the raw evidence.",
                                      source="hook:PostToolUse")

    def expire_claim(self):
        with Store(self.home) as store:
            store._connection.execute(
                "UPDATE observation_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE status='running'")
            return tuple(store._connection.execute(
                "SELECT id,lease_token,attempt_count FROM observation_jobs WHERE status='running'").fetchone())

    @staticmethod
    def skipped(_request):
        return {"output": {"notes": [], "disposition": "skipped"}, "evidence": {
            "thread_start": {"thread_id": "worker", "model": MODEL,
                             "reasoning_effort": REASONING_EFFORT, "model_provider": "openai"},
            "turn_started": {"thread_id": "worker", "turn_id": "turn"},
            "turn_completed": True, "no_tools": True, "rerouted": False}}

    def assert_expired_claim_untouched_and_recoverable(self, result, claim):
        self.assertEqual("lease_expired", result["code"], result)
        with Store(self.home) as store:
            row = store._connection.execute(
                "SELECT status,lease_token,attempt_count,error_code FROM observation_jobs WHERE id=?",
                (claim[0],)).fetchone()
            self.assertEqual(("running", claim[1], claim[2], None), tuple(row))
            job = store.observation_job_status(self.project, claim[0])
            self.assertEqual([], job["failure_receipts"])
            self.assertEqual([], job["output_ids"])
            self.assertIsNone(store.get(self.project, [self.raw["id"]])[0]["superseded_by"])
        configure(self.home, jev_filter_enabled=False)
        recovered = process_pending(self.project, self.home, runner=self.skipped)
        self.assertEqual("skipped", recovered["status"], recovered)
        self.assertEqual(claim[0], recovered["job_id"])
        with Store(self.home) as store:
            self.assertEqual(claim[2] + 1,
                             store.observation_job_status(self.project, claim[0])["attempt_count"])

    def test_jev_filter_timeout_before_expiry_persists_runner_failure(self):
        configure(self.home, jev_filter_enabled=True)
        runner = mock.Mock(side_effect=AssertionError("must not generate"))
        result = process_pending(self.project, self.home, runner=runner,
                                 jev_evaluator=mock.Mock(side_effect=JevFilterError("jev_filter_timeout")))
        self.assertEqual("runner_failure", result["code"])
        self.assertEqual("jev_filter_timeout", result["reason_code"])
        runner.assert_not_called()
        with Store(self.home) as store:
            job = store.observation_job_status(self.project, result["job_id"])
            self.assertEqual("failed", job["status"])
            self.assertEqual("runner_failure", job["error_code"])
            self.assertEqual("jev_filter_timeout", job["failure_receipts"][-1]["reason_code"])

    def test_jev_filter_timeout_after_expiry_returns_lease_expired(self):
        configure(self.home, jev_filter_enabled=True)
        claims = []
        def evaluator(_payload):
            claims.append(self.expire_claim())
            raise JevFilterError("jev_filter_timeout")
        runner = mock.Mock(side_effect=AssertionError("must not generate"))
        result = process_pending(self.project, self.home, runner=runner, jev_evaluator=evaluator)
        runner.assert_not_called()
        self.assertEqual("jev_filter_timeout", result["jev_filter"]["error_code"])
        self.assert_expired_claim_untouched_and_recoverable(result, claims[0])

    def test_storage_failure_after_expiry_returns_lease_expired(self):
        claims = []
        def finish(*_args, **_kwargs):
            claims.append(self.expire_claim())
            raise StoreError("storage operation failed")
        with mock.patch.object(Store, "finish_observation_batch", side_effect=finish):
            result = process_pending(self.project, self.home, runner=self.skipped)
        self.assert_expired_claim_untouched_and_recoverable(result, claims[0])

    def test_unexpected_runner_failure_after_expiry_returns_lease_expired(self):
        claims = []
        def runner(_request):
            claims.append(self.expire_claim())
            raise RuntimeError("untrusted runner detail")
        result = process_pending(self.project, self.home, runner=runner)
        self.assert_expired_claim_untouched_and_recoverable(result, claims[0])

    def test_expiry_does_not_relax_hard_runner_reasons(self):
        for reason in ("native_policy", "native_auth", "native_bad_request", "native_usage_limit",
                       "native_turn_cancelled", "jev_filter_credentials"):
            with self.subTest(reason=reason):
                def runner(_request):
                    self.expire_claim()
                    raise ProcessorFailure("runner_failure", reason_code=reason)
                result = process_pending(self.project, self.home, runner=runner)
                self.assertEqual("runner_failure", result["code"], result)
                self.assertEqual(reason, result["reason_code"])
