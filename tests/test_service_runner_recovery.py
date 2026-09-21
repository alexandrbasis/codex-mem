from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem.processor import MODEL, PROCESSOR_ID, REASONING_EFFORT, process_pending
from codex_mem.service import SERVICE_STATE_FILENAME, enqueue, run_service, service_status
from codex_mem.store import Store


class ServiceRunnerRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.workspace = str(self.project.resolve())
        self.data_dir = self.root / "memory"
        self.now = 1_000.0
        configure(self.data_dir, capture_scope="selected", included_projects=[self.project],
                  semantic_enabled=False)
        self.seed(self.data_dir)

    def seed(self, data_dir: Path, session: str = "runner-recovery") -> None:
        with Store(data_dir) as store:
            store.remember(self.project, "Source", "Verified project evidence.",
                           source="hook:PostToolUse", session_id=session)

    def clock(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.now += delay

    def state(self) -> dict:
        return json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())

    def failing_processor(self, reason: str | None, calls: list, *, data_dir: Path | None = None):
        def processor(project: str, **kwargs: object) -> dict:
            calls.append((self.now, dict(kwargs)))
            with Store(data_dir or self.data_dir) as store:
                job = store.claim_observation_batch(project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                    retry_failed=kwargs["retry_failed"], retry_job_id=kwargs.get("retry_job_id"),
                    retry_error_code=kwargs.get("retry_error_code"),
                    retry_attempt_count=kwargs.get("retry_attempt_count"))
                if job is None:
                    return {"status": "idle"}
                store.fail_observation_batch(project, job["job_id"], job["lease_token"],
                                             code="runner_failure", reason_code=reason)
                return {"status": "failed", "code": "runner_failure", "job_id": job["job_id"],
                        "reason_code": reason}
        return processor

    def legacy_block(self) -> None:
        enqueue(self.project, self.data_dir, clock=self.clock)
        state = self.state()
        record = state["projects"][self.workspace]
        record.update(blocked=True, last_code="runner_failure")
        for field in ("last_failure_job", "runner_recovery_checked", "last_failure_detail", "last_failure_at",
                      "retry_error_code", "retry_attempt_count"):
            record.pop(field, None)
        (self.data_dir / SERVICE_STATE_FILENAME).write_text(json.dumps(state))

    @staticmethod
    def skipped_receipt() -> dict:
        return {"output": {"notes": [], "disposition": "skipped"},
                "evidence": {"thread_start": {"thread_id": "fresh-worker", "model": MODEL,
                                               "reasoning_effort": REASONING_EFFORT,
                                               "model_provider": "openai"},
                             "turn_started": {"thread_id": "fresh-worker", "turn_id": "fresh-turn"},
                             "turn_completed": True, "no_tools": True, "rerouted": False}}

    def test_durable_unknown_runner_failure_recovers_exact_batch_once(self) -> None:
        enqueue(self.project, self.data_dir, clock=self.clock)
        calls = []
        job_ids = []

        def processor(project: str, **kwargs: object) -> dict:
            calls.append((self.now, kwargs["retry_failed"], kwargs.get("retry_job_id")))
            with Store(self.data_dir) as store:
                job = store.claim_observation_batch(project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                    retry_failed=kwargs["retry_failed"], retry_job_id=kwargs.get("retry_job_id"))
                self.assertEqual("running", job["status"])
                job_ids.append(job["job_id"])
                if len(calls) == 1:
                    store.fail_observation_batch(project, job["job_id"], job["lease_token"],
                                                 code="runner_failure")
                    return {"status": "failed", "code": "runner_failure", "job_id": job["job_id"]}
                store.finish_observation_batch(project, job["job_id"], job["lease_token"],
                                               disposition="skipped")
                return {"status": "idle"}

        result = run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep,
                             poll_interval=5, retry_backoff=5, max_cycles=3)
        self.assertTrue(job_ids, "the failure must reach the durable Store receipt")
        self.assertEqual("cycle_limit", result["status"])
        self.assertEqual([(1000.0, False, None), (1005.0, False, job_ids[0])], calls)
        self.assertEqual([job_ids[0], job_ids[0]], job_ids)
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["blocked_projects"])

    def test_transient_retry_budget_and_backoff_survive_restart_and_capture(self) -> None:
        enqueue(self.project, self.data_dir, clock=self.clock)
        calls = []
        processor = self.failing_processor("jev_filter_invalid_response", calls)
        run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep,
                    retry_backoff=5, max_cycles=1)
        record = self.state()["projects"][self.workspace]
        job_id = record["retry_job_id"]
        self.assertEqual((1005.0, 1), (record["due_at"], record["retry_attempt_count"]))
        self.now = 1001.0
        enqueue(self.project, self.data_dir, clock=self.clock)
        self.assertEqual(1005.0, self.state()["projects"][self.workspace]["due_at"])
        run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep,
                    retry_backoff=5, poll_interval=20, max_cycles=2)
        self.assertEqual(1015.0, self.state()["projects"][self.workspace]["due_at"])
        self.now = 1006.0
        enqueue(self.project, self.data_dir, clock=self.clock)
        result = run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep,
                             retry_backoff=5, poll_interval=20, max_cycles=3)
        self.assertEqual("halted", result["status"])
        self.assertEqual([1000.0, 1005.0, 1015.0], [row[0] for row in calls])
        self.assertEqual([None, 1, 2], [row[1].get("retry_attempt_count") for row in calls])
        self.assertEqual([None, job_id, job_id], [row[1].get("retry_job_id") for row in calls])
        self.assertTrue(all(row[1]["retry_failed"] is False for row in calls))
        self.assertEqual(1, service_status(self.data_dir, clock=self.clock)["blocked_projects"])
        run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep, max_cycles=3)
        self.assertEqual(3, len(calls))
        with Store(self.data_dir) as store:
            self.assertEqual(3, store.observation_job_status(self.project, job_id)["attempt_count"])

    def test_unknown_runner_failure_stops_after_one_retry(self) -> None:
        enqueue(self.project, self.data_dir, clock=self.clock)
        calls = []
        processor = self.failing_processor(None, calls)
        result = run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep,
                             poll_interval=5, max_cycles=10)
        self.assertEqual("halted", result["status"])
        self.assertEqual([1000.0, 1005.0], [row[0] for row in calls])
        run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep, max_cycles=3)
        self.assertEqual(2, len(calls))

    def test_permanent_runner_reasons_and_cancellation_never_auto_retry(self) -> None:
        reasons = ("jev_filter_credentials", "jev_filter_invalid_input", "native_auth",
                   "native_usage_limit", "native_context_limit", "native_bad_request", "native_policy",
                   "native_turn_cancelled")
        for index, reason in enumerate(reasons):
            with self.subTest(reason=reason):
                data_dir = self.root / f"permanent-{index}"
                configure(data_dir, capture_scope="selected", included_projects=[self.project],
                          semantic_enabled=False)
                self.seed(data_dir)
                enqueue(self.project, data_dir, clock=self.clock)
                calls = []
                result = run_service(data_dir, processor=self.failing_processor(reason, calls, data_dir=data_dir),
                    clock=self.clock, sleeper=self.sleep, poll_interval=5, max_cycles=10)
                self.assertEqual("halted", result["status"])
                self.assertEqual(1, len(calls))
                failure = service_status(data_dir, clock=self.clock)["failures"][self.workspace]
                self.assertEqual(reason, failure["detail"]["reason_code"])

    def test_runner_retry_preserves_existing_invalid_response_quarantine(self) -> None:
        with Store(self.data_dir) as store:
            rejected = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            store.fail_observation_batch(self.project, rejected["job_id"], rejected["lease_token"],
                                         code="invalid_response", reason_code="invalid_json")
        self.seed(self.data_dir, session="fresh-runner-evidence")
        enqueue(self.project, self.data_dir, clock=self.clock)
        calls = []
        run_service(self.data_dir, processor=self.failing_processor("native_connection_error", calls),
                    clock=self.clock, sleeper=self.sleep, poll_interval=5, max_cycles=10)
        self.assertEqual(3, len(calls))
        with Store(self.data_dir) as store:
            current = store.observation_job_status(self.project, rejected["job_id"])
        self.assertEqual(("failed", "invalid_response", 1),
                         (current["status"], current["error_code"], current["attempt_count"]))
        self.assertTrue(all(row[1].get("retry_job_id") != rejected["job_id"] for row in calls))

    def test_runner_failure_requires_matching_persisted_job(self) -> None:
        with Store(self.data_dir) as store:
            rejected = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            store.fail_observation_batch(self.project, rejected["job_id"], rejected["lease_token"],
                                         code="invalid_response")
        enqueue(self.project, self.data_dir, clock=self.clock)
        calls = []
        def processor(*args: object, **kwargs: object) -> dict:
            calls.append(kwargs)
            return {"status": "failed", "code": "runner_failure", "job_id": rejected["job_id"],
                    "reason_code": "native_server_error"}
        result = run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep,
                             poll_interval=5, max_cycles=10)
        self.assertEqual("halted", result["status"])
        self.assertEqual(1, len(calls))

    def test_permanent_receipt_narrows_retry_when_legacy_storage_has_no_reason(self) -> None:
        with Store(self.data_dir) as store:
            failed = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            store.fail_observation_batch(self.project, failed["job_id"], failed["lease_token"],
                                         code="runner_failure")
        enqueue(self.project, self.data_dir, clock=self.clock)
        calls = []
        def processor(*args: object, **kwargs: object) -> dict:
            calls.append(kwargs)
            return {"status": "failed", "code": "runner_failure", "job_id": failed["job_id"],
                    "reason_code": "native_auth"}
        result = run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep,
                             poll_interval=5, max_cycles=10)
        self.assertEqual("halted", result["status"])
        self.assertEqual(1, len(calls))

    def test_legacy_runner_block_gets_one_finite_guarded_recovery(self) -> None:
        with Store(self.data_dir) as store:
            job = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            store.fail_observation_batch(self.project, job["job_id"], job["lease_token"], code="runner_failure")
        self.legacy_block()
        calls = []
        processor = self.failing_processor(None, calls)
        run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep,
                    poll_interval=5, max_cycles=1)
        record = self.state()["projects"][self.workspace]
        self.assertFalse(record["blocked"])
        self.assertIsNone(record["last_failure_at"])
        self.assertEqual((job["job_id"], "runner_failure", 1),
                         (record["retry_job_id"], record["retry_error_code"], record["retry_attempt_count"]))
        result = run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep,
                             poll_interval=5, max_cycles=5)
        self.assertEqual("halted", result["status"])
        run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep,
                    poll_interval=5, max_cycles=5)
        self.assertEqual(1, len(calls))
        self.assertFalse(calls[0][1]["retry_failed"])

    def test_legacy_recovery_preserves_excluded_scope_and_disabled_processing(self) -> None:
        with Store(self.data_dir) as store:
            job = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            store.fail_observation_batch(self.project, job["job_id"], job["lease_token"], code="runner_failure")
        self.legacy_block()
        configure(self.data_dir, included_projects=[])
        calls = []
        processor = self.failing_processor(None, calls)
        run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep, max_cycles=2)
        record = self.state()["projects"][self.workspace]
        self.assertTrue(record["blocked"])
        self.assertFalse(record["runner_recovery_checked"])
        configure(self.data_dir, included_projects=[self.project], processor_enabled=False)
        run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep, max_cycles=2)
        self.assertTrue(self.state()["projects"][self.workspace]["blocked"])
        self.assertEqual([], calls)

    def test_legacy_ambiguous_failures_do_not_guess_a_batch(self) -> None:
        with Store(self.data_dir) as store:
            first = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            store.fail_observation_batch(self.project, first["job_id"], first["lease_token"], code="runner_failure")
        self.seed(self.data_dir, session="another-runner-failure")
        with Store(self.data_dir) as store:
            second = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            store.fail_observation_batch(self.project, second["job_id"], second["lease_token"], code="runner_failure")
        self.legacy_block()
        calls = []
        run_service(self.data_dir, processor=self.failing_processor(None, calls),
                    clock=self.clock, sleeper=self.sleep, max_cycles=3)
        self.assertEqual([], calls)
        record = self.state()["projects"][self.workspace]
        self.assertTrue(record["blocked"])
        self.assertTrue(record["runner_recovery_checked"])
        self.assertIsNone(record["retry_job_id"])

    def test_changed_runner_snapshot_cannot_retry_a_later_auth_failure(self) -> None:
        enqueue(self.project, self.data_dir, clock=self.clock)
        calls = []
        processor = self.failing_processor("native_connection_error", calls)
        run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep, max_cycles=1)
        job_id = self.state()["projects"][self.workspace]["retry_job_id"]
        with Store(self.data_dir) as store:
            changed = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                                                   retry_job_id=job_id)
            store.fail_observation_batch(self.project, job_id, changed["lease_token"],
                                         code="runner_failure", reason_code="native_auth")
        runner = mock.Mock(side_effect=AssertionError("the changed authorization must not call a model"))
        result = run_service(self.data_dir,
            processor=lambda project, **kwargs: process_pending(project, runner=runner, **kwargs),
            clock=self.clock, sleeper=self.sleep, poll_interval=5, max_cycles=3)
        with Store(self.data_dir) as store:
            current = store.observation_job_status(self.project, job_id)
        self.assertEqual(2, current["attempt_count"])
        self.assertEqual("native_auth", current["failure_receipts"][-1]["reason_code"])
        runner.assert_not_called()
        self.assertEqual("runner_failure", result["code"])
        failure = service_status(self.data_dir, clock=self.clock)["failures"][self.workspace]
        self.assertEqual("native_auth", failure["detail"]["reason_code"])
        self.assertEqual(job_id, self.state()["projects"][self.workspace]["last_failure_job"])

    def test_concurrently_completed_retry_does_not_reblock_and_drains_fresh_work(self) -> None:
        enqueue(self.project, self.data_dir, clock=self.clock)
        run_service(self.data_dir, processor=self.failing_processor("native_connection_error", []),
                    clock=self.clock, sleeper=self.sleep, max_cycles=1)
        job_id = self.state()["projects"][self.workspace]["retry_job_id"]
        with Store(self.data_dir) as store:
            repaired = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                                                    retry_job_id=job_id)
            store.finish_observation_batch(self.project, job_id, repaired["lease_token"], disposition="skipped")
        self.seed(self.data_dir, session="fresh-after-concurrent-repair")
        calls = []
        def runner(request: dict) -> dict:
            calls.append(request)
            return self.skipped_receipt()
        result = run_service(self.data_dir,
            processor=lambda project, **kwargs: process_pending(project, runner=runner, **kwargs),
            clock=self.clock, sleeper=self.sleep, poll_interval=5, max_cycles=4)
        self.assertEqual("cycle_limit", result["status"])
        self.assertEqual(1, len(calls))
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["blocked_projects"])
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["queued_projects"])
        with Store(self.data_dir) as store:
            repaired_status = store.observation_job_status(self.project, job_id)
        self.assertEqual(("skipped", 2), (repaired_status["status"], repaired_status["attempt_count"]))

    def test_concurrent_running_retry_waits_for_lease_before_normal_reclamation(self) -> None:
        enqueue(self.project, self.data_dir, clock=self.clock)
        run_service(self.data_dir, processor=self.failing_processor("native_connection_error", []),
                    clock=self.clock, sleeper=self.sleep, max_cycles=1)
        job_id = self.state()["projects"][self.workspace]["retry_job_id"]
        with Store(self.data_dir) as store:
            store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                                          retry_job_id=job_id)
            store._connection.execute("UPDATE observation_jobs SET lease_expires_at=? WHERE id=?",
                                      ("1970-01-01T00:16:55Z", job_id))
        runner = mock.Mock(return_value=self.skipped_receipt())
        processor = lambda project, **kwargs: process_pending(project, runner=runner, **kwargs)
        self.now = 1005.0
        result = run_service(self.data_dir, processor=processor, clock=self.clock,
                             sleeper=self.sleep, poll_interval=5, max_cycles=1)
        self.assertEqual("cycle_limit", result["status"])
        runner.assert_not_called()
        record = self.state()["projects"][self.workspace]
        self.assertFalse(record["blocked"])
        self.assertEqual(1015.0, record["due_at"])
        self.assertEqual(job_id, record["retry_job_id"])
        self.now = 1015.0
        run_service(self.data_dir, processor=processor, clock=self.clock,
                    sleeper=self.sleep, poll_interval=5, max_cycles=6)
        self.assertEqual(1, runner.call_count)
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["blocked_projects"])
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["queued_projects"])
        with Store(self.data_dir) as store:
            current = store.observation_job_status(self.project, job_id)
        self.assertEqual(("skipped", 3), (current["status"], current["attempt_count"]))


if __name__ == "__main__":
    unittest.main()
