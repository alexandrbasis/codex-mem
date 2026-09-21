"""Exact recovery authorizations cannot silently widen or repeat a model call."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem.processor import MODEL, PROCESSOR_ID, REASONING_EFFORT, ProcessorFailure
from codex_mem import processor
from codex_mem.store import Store


class ProcessorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.foreign = self.root / "foreign"
        self.project.mkdir()
        self.foreign.mkdir()
        self.base = self.root / "memory"
        configure(self.base, capture_scope="selected", included_projects=[self.project])
        self.jobs = {}
        self.sources = {}
        with Store(self.base) as store:
            for name, owner, code in (("rejected", self.project, "invalid_response"),
                                      ("other", self.project, "invalid_response"),
                                      ("foreign", self.foreign, "invalid_response")):
                raw = store.remember(owner, name, "private raw evidence", source="hook:PostToolUse",
                                     session_id=name)
                job = store.claim_observation_batch(owner, PROCESSOR_ID, MODEL, REASONING_EFFORT)
                store.fail_observation_batch(owner, job["job_id"], job["lease_token"], code)
                self.jobs[name] = job["job_id"]
                self.sources[name] = raw["id"]
            store.remember(self.project, "fresh", "new ordinary source", source="hook:PostToolUse",
                           session_id="fresh")

    @staticmethod
    def valid_receipt(request):
        return {"output": {"notes": [{"title": "Recovered finding", "body": "A useful supported finding.",
                                       "tags": [], "source_ids": [request["sources"][0]["id"]]}],
                            "disposition": "processed"},
                "evidence": {"thread_start": {"thread_id": "worker", "model": MODEL,
                                               "reasoning_effort": REASONING_EFFORT,
                                               "model_provider": "openai"},
                             "turn_started": {"thread_id": "worker", "turn_id": "turn"},
                             "turn_completed": True, "no_tools": True, "rerouted": False}}

    def recover(self, **kwargs):
        arguments = dict(job_id=self.jobs["rejected"], expected_error_code="invalid_response",
                         expected_attempt_count=1, runner=self.valid_receipt)
        arguments.update(kwargs)
        return processor.recover_failed_batch(self.project, self.base, **arguments)

    def test_recovery_consumes_only_exact_snapshot_once_and_keeps_sources(self):
        calls = []
        def runner(request):
            calls.append(request)
            return self.valid_receipt(request)
        receipt = self.recover(runner=runner)
        self.assertEqual("processed", receipt["status"], receipt)
        self.assertEqual(("failed", 1), (receipt["before"]["status"], receipt["before"]["attempt_count"]))
        self.assertEqual(("processed", 2), (receipt["after"]["status"], receipt["after"]["attempt_count"]))
        self.assertEqual(1, receipt["before"]["source_count"])
        replay = self.recover(runner=runner)
        self.assertEqual("recovery_unavailable", replay["code"])
        self.assertEqual(1, len(calls))
        self.assertNotIn("private raw evidence", json.dumps(receipt))
        with Store(self.base) as store:
            rows = {row["id"]: (row["status"], row["attempt_count"])
                    for row in store._connection.execute("SELECT id,status,attempt_count FROM observation_jobs")}
            self.assertEqual(("failed", 1), rows[self.jobs["other"]])
            self.assertEqual(("failed", 1), rows[self.jobs["foreign"]])
            self.assertEqual(3, len(rows))
            self.assertEqual("private raw evidence", store.get(self.project, [self.sources["rejected"]])[0]["body"])

    def test_repeated_failed_recovery_does_not_retry_same_authorization(self):
        runner = mock.Mock(side_effect=ProcessorFailure("invalid_response", reason_code="invalid_note_shape"))
        first = self.recover(runner=runner)
        self.assertEqual("failed", first["status"])
        self.assertEqual("invalid_response", first["code"])
        self.assertEqual(2, first["after"]["attempt_count"])
        second = self.recover(runner=runner)
        self.assertEqual("recovery_unavailable", second["code"])
        self.assertEqual(1, runner.call_count)
        with Store(self.base) as store:
            self.assertIsNone(store.get(self.project, [self.sources["rejected"]])[0]["superseded_by"])
            saved = store.observation_job_status(self.project, self.jobs["rejected"])
            self.assertEqual("invalid_note_shape", saved["failure_receipts"][-1]["reason_code"])
            self.assertEqual("invalid_response", saved["failure_receipts"][-1]["error_code"])
            self.assertIsNone(saved["failure_receipts"][0]["reason_code"])

    def test_success_reconciles_quarantine_and_preserves_unrelated_blocker(self):
        from codex_mem.service import SERVICE_STATE_FILENAME, enqueue, run_service, service_status
        enqueue(self.project, self.base)
        run_service(self.base, processor=lambda *_a, **_k: {
            "status": "failed", "code": "invalid_response", "job_id": self.jobs["rejected"],
            "reason_code": "source_attribution_conflict"}, max_cycles=1)
        first = self.recover()
        self.assertEqual({"status": "reconciled", "blocked": False, "rejected_batches": 1},
                         first["service_reconciliation"])
        state_path = self.base / SERVICE_STATE_FILENAME
        state = json.loads(state_path.read_text())
        record = state["projects"][str(self.project.resolve())]
        self.assertEqual(self.jobs["other"], record["last_rejected_job"])
        record["blocked"] = True
        record["last_code"] = "index_failure"
        state_path.write_text(json.dumps(state))
        last = self.recover(job_id=self.jobs["other"])
        self.assertEqual({"status": "reconciled", "blocked": True, "rejected_batches": 0},
                         last["service_reconciliation"])
        status = service_status(self.base)
        self.assertEqual(0, status["quarantined_batches"])
        self.assertEqual(1, status["blocked_projects"])

    def test_success_clears_matching_operational_blocker(self):
        from codex_mem.service import enqueue, run_service, service_status
        with Store(self.base) as store:
            store._connection.execute("UPDATE observation_jobs SET error_code='runner_failure' WHERE id=?",
                                      (self.jobs["rejected"],))
        enqueue(self.project, self.base)
        run_service(self.base, processor=lambda *_a, **_k: {"status": "failed", "code": "runner_failure"},
                    max_cycles=1)
        result = self.recover(expected_error_code="runner_failure")
        self.assertFalse(result["service_reconciliation"]["blocked"])
        self.assertEqual(0, service_status(self.base)["blocked_projects"])

    def test_missing_foreign_stale_and_changed_failures_do_not_claim_fresh_work(self):
        runner = mock.Mock(side_effect=AssertionError("must not invoke model"))
        for changes in ({"job_id": "missing"}, {"job_id": self.jobs["foreign"]},
                        {"expected_attempt_count": 2}, {"expected_error_code": "timeout"}):
            with self.subTest(changes=changes):
                self.assertEqual("recovery_unavailable", self.recover(runner=runner, **changes)["code"])
        runner.assert_not_called()

    def test_atomic_claim_rechecks_attempt_after_read_before_invoking_model(self):
        runner = mock.Mock(side_effect=AssertionError("must not invoke model"))
        original = Store.claim_observation_batch
        def claim(store, *args, **kwargs):
            store._connection.execute("UPDATE observation_jobs SET attempt_count=2 WHERE id=?",
                                      (self.jobs["rejected"],))
            return original(store, *args, **kwargs)
        with mock.patch.object(Store, "claim_observation_batch", claim):
            receipt = self.recover(runner=runner)
        self.assertEqual("recovery_unavailable", receipt["code"])
        runner.assert_not_called()

    def test_disabled_processor_is_preserved(self):
        configure(self.base, processor_enabled=False)
        runner = mock.Mock()
        self.assertEqual("processor_disabled", self.recover(runner=runner)["code"])
        runner.assert_not_called()

    def test_active_project_claim_prevents_parallel_manual_recovery(self):
        with Store(self.base) as store:
            active = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            self.assertIsNotNone(active)
        runner = mock.Mock()
        self.assertEqual("recovery_unavailable", self.recover(runner=runner)["code"])
        runner.assert_not_called()

    def test_manual_recovery_lease_defers_normal_worker_and_keeps_fresh_sources_unclaimed(self):
        calls = []
        def runner(request):
            normal_runner = mock.Mock(side_effect=AssertionError("overlapping model call"))
            normal = processor.process_pending(self.project, self.base, runner=normal_runner)
            calls.append(normal)
            self.assertEqual("deferred", normal["status"])
            normal_runner.assert_not_called()
            with Store(self.base) as store:
                self.assertEqual(1, store._connection.execute(
                    "SELECT COUNT(*) FROM observation_jobs WHERE project=? AND status='running'",
                    (str(self.project.resolve()),),
                ).fetchone()[0])
            return self.valid_receipt(request)
        self.assertEqual("processed", self.recover(runner=runner)["status"])
        self.assertEqual(1, len(calls))

    def test_recovery_preserves_another_jobs_timeout_retry_and_backoff(self):
        from codex_mem.service import SERVICE_STATE_FILENAME, enqueue, run_service
        enqueue(self.project, self.base)
        state_path = self.base / SERVICE_STATE_FILENAME
        state = json.loads(state_path.read_text())
        record = state["projects"][str(self.project.resolve())]
        record.update(last_code="timeout", attempts=2, due_at=4_000_000_000.0,
                      retry_job_id=self.jobs["other"], last_failure_at=123.0)
        state_path.write_text(json.dumps(state))
        before = dict(record)
        self.assertEqual("processed", self.recover()["status"])
        after = json.loads(state_path.read_text())["projects"][str(self.project.resolve())]
        for key in ("attempts", "due_at", "retry_requested", "retry_job_id", "last_code", "last_failure_at"):
            self.assertEqual(before[key], after[key], key)

    def test_failed_recovery_reconciles_new_failure_and_remaining_quarantine(self):
        from codex_mem.service import SERVICE_STATE_FILENAME, enqueue, run_service
        enqueue(self.project, self.base)
        run_service(self.base, processor=lambda *_a, **_k: {
            "status": "failed", "code": "invalid_response", "job_id": self.jobs["rejected"],
            "reason_code": "source_attribution_conflict"}, max_cycles=1)
        runner = mock.Mock(side_effect=ProcessorFailure("runner_failure"))
        receipt = self.recover(runner=runner)
        self.assertEqual("runner_failure", receipt["code"])
        self.assertEqual({"status": "reconciled", "blocked": True, "rejected_batches": 1},
                         receipt["service_reconciliation"])
        record = json.loads((self.base / SERVICE_STATE_FILENAME).read_text())["projects"][str(self.project.resolve())]
        self.assertEqual("runner_failure", record["last_code"])
        self.assertEqual(self.jobs["other"], record["last_rejected_job"])
        self.assertIsNone(record["last_rejected_reason"])
        self.assertEqual(1, runner.call_count)

    def test_diagnostic_reason_never_persists_untrusted_values(self):
        with Store(self.base) as store:
            active = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            store.fail_observation_batch(self.project, active["job_id"], active["lease_token"],
                                         "invalid_response", reason_code={"secret-marker": "source body"})
            receipt = store.observation_job_status(self.project, active["job_id"])
        self.assertIsNone(receipt["failure_receipts"][-1]["reason_code"])
        self.assertNotIn("secret-marker", json.dumps(receipt))

    def test_cli_requires_exact_snapshot_and_emits_receipt(self):
        from codex_mem.cli import main
        out = io.StringIO()
        with mock.patch("codex_mem.processor.recover_failed_batch", return_value={"status": "processed"}) as recover:
            with contextlib.redirect_stdout(out):
                status = main(["--data-dir", str(self.base), "recover-batch", "--project", str(self.project),
                               "--job-id", self.jobs["rejected"], "--expected-error", "invalid_response",
                               "--expected-attempt", "1"])
        self.assertEqual(0, status)
        self.assertEqual(1, recover.call_args.kwargs["expected_attempt_count"])
        self.assertEqual("invalid_response", recover.call_args.kwargs["expected_error_code"])
        self.assertEqual({"status": "processed"}, json.loads(out.getvalue()))


if __name__ == "__main__":
    unittest.main()
