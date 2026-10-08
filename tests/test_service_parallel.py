from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem.processor import MODEL, PROCESSOR_ID, REASONING_EFFORT
from codex_mem.service import SERVICE_STATE_FILENAME, enqueue, run_service, service_status, stop_service
from codex_mem.store import Store


class ParallelServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = (self.root / "project").resolve()
        self.project.mkdir()
        self.data_dir = self.root / "memory"
        self.now = 1000.0
        configure(self.data_dir, capture_scope="selected", included_projects=[self.project], semantic_enabled=False)

    def clock(self):
        return self.now

    def sleep(self, delay):
        self.now += delay

    def seed(self, session):
        with Store(self.data_dir) as store:
            store.remember(self.project, "Source", "Verified evidence for " + session,
                           source="hook:PostToolUse", session_id=session)

    def state(self):
        return json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())

    def run_worker(self, processor, **kwargs):
        return run_service(self.data_dir, processor=processor, processor_workers=kwargs.pop("processor_workers", 2),
                           clock=self.clock, sleeper=self.sleep, poll_interval=5, **kwargs)

    def claim(self, store, project, kwargs):
        return store.claim_observation_batch(project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
            parallel_sessions=kwargs.get("parallel_sessions", False), retry_failed=kwargs["retry_failed"],
            retry_job_id=kwargs.get("retry_job_id"), retry_error_code=kwargs.get("retry_error_code"),
            retry_attempt_count=kwargs.get("retry_attempt_count"))

    def test_distinct_sessions_overlap_and_indexing_stays_on_coordinator(self):
        for session in ("a", "b"):
            self.seed(session)
        configure(self.data_dir, semantic_enabled=True)
        enqueue(self.project, self.data_dir, clock=self.clock)
        barrier = threading.Barrier(2)
        seen = []
        coordinator = threading.get_ident()
        index_threads = []

        def processor(project, **kwargs):
            with Store(self.data_dir) as store:
                job = self.claim(store, project, kwargs)
                if job is None:
                    return {"status": "idle"}
                seen.append(job["session_id"])
                barrier.wait(timeout=5)
                store.finish_observation_batch(project, job["job_id"], job["lease_token"], disposition="skipped")
                return {"status": "skipped", "job_id": job["job_id"]}

        def indexer(*args, **kwargs):
            index_threads.append(threading.get_ident())
            return False

        result = self.run_worker(processor, max_cycles=2, indexer=indexer)
        self.assertEqual("cycle_limit", result["status"])
        self.assertCountEqual(["a", "b"], seen)
        self.assertEqual([coordinator, coordinator], index_threads)
        self.assertEqual({}, self.state()["projects"])

    def test_success_and_quarantine_are_both_committed(self):
        for session in ("bad", "good"):
            self.seed(session)
        enqueue(self.project, self.data_dir, clock=self.clock)
        barrier = threading.Barrier(2)

        def processor(project, **kwargs):
            with Store(self.data_dir) as store:
                job = self.claim(store, project, kwargs)
                if job is None:
                    return {"status": "idle"}
                barrier.wait(timeout=5)
                if job["session_id"] == "bad":
                    store.fail_observation_batch(project, job["job_id"], job["lease_token"],
                                                 code="invalid_response", reason_code="invalid_note_shape")
                    return {"status": "failed", "code": "invalid_response", "reason_code": "invalid_note_shape", "job_id": job["job_id"]}
                store.finish_observation_batch(project, job["job_id"], job["lease_token"], disposition="skipped")
                return {"status": "skipped", "job_id": job["job_id"]}

        self.run_worker(processor, max_cycles=2)
        status = service_status(self.data_dir, clock=self.clock)
        self.assertEqual(1, status["quarantined_batches"])
        self.assertEqual(0, status["blocked_projects"])
        self.assertEqual("rejected_batches", self.state()["projects"][str(self.project)]["parked"])
        with Store(self.data_dir) as store:
            jobs = store.status(self.project)["observation_jobs"]
            self.assertEqual(1, jobs["skipped"])
            self.assertEqual(1, jobs["failed"])

    def test_two_exact_failures_survive_restart_and_serial_configuration(self):
        for session in ("a", "b"):
            self.seed(session)
        enqueue(self.project, self.data_dir, clock=self.clock)
        barrier = threading.Barrier(2)
        failed_ids = []
        retried_ids = []

        def processor(project, **kwargs):
            with Store(self.data_dir) as store:
                job = self.claim(store, project, kwargs)
                if job is None:
                    return {"status": "idle"}
                if kwargs.get("retry_job_id") is None:
                    barrier.wait(timeout=5)
                    failed_ids.append(job["job_id"])
                    store.fail_observation_batch(project, job["job_id"], job["lease_token"],
                                                 code="runner_failure", reason_code="native_rate_limit")
                    return {"status": "failed", "code": "runner_failure", "reason_code": "native_rate_limit", "job_id": job["job_id"]}
                self.assertFalse(kwargs["retry_failed"])
                self.assertEqual("runner_failure", kwargs["retry_error_code"])
                self.assertEqual(1, kwargs["retry_attempt_count"])
                retried_ids.append(kwargs["retry_job_id"])
                store.finish_observation_batch(project, job["job_id"], job["lease_token"], disposition="skipped")
                return {"status": "skipped", "job_id": job["job_id"]}

        self.run_worker(processor, max_cycles=1)
        record = self.state()["projects"][str(self.project)]
        self.assertEqual(1, len(record["pending_retries"]))
        self.assertCountEqual(failed_ids, [record["retry_job_id"], record["pending_retries"][0]["job_id"]])
        self.run_worker(processor, processor_workers=1, max_cycles=6)
        self.assertCountEqual(failed_ids, retried_ids)
        self.assertEqual({}, self.state()["projects"])

    def test_stop_waits_for_all_active_calls_and_preserves_concurrent_enqueue(self):
        enqueue(self.project, self.data_dir, clock=self.clock)
        barrier = threading.Barrier(2)
        completed = []

        def processor(project, **kwargs):
            barrier.wait(timeout=5)
            if threading.current_thread().name.endswith("_0"):
                enqueue(project, self.data_dir, clock=self.clock)
                self.assertEqual("stopping", stop_service(self.data_dir, clock=self.clock)["status"])
            completed.append(threading.get_ident())
            return {"status": "idle"}

        result = self.run_worker(processor, max_cycles=10)
        self.assertEqual("stopped", result["status"])
        self.assertEqual(2, len(completed))
        record = self.state()["projects"][str(self.project)]
        self.assertEqual(2, record["generation"])
        self.assertIsNone(record["inflight_generation"])

    def test_rate_limit_reduces_workers_then_recovers_after_four_successes(self):
        enqueue(self.project, self.data_dir, clock=self.clock)
        counts = []
        calls = 0
        lock = threading.Lock()
        barrier = threading.Barrier(2)
        from codex_mem.service import _record_workers

        def metadata(base, configured, effective, active):
            counts.append((configured, effective, active))
            return _record_workers(base, configured, effective, active)

        def processor(project, **kwargs):
            nonlocal calls
            with lock:
                calls += 1
                number = calls
            if number <= 2:
                barrier.wait(timeout=5)
                # A deferred transient call reduces concurrency without a
                # manufactured durable failure in this scheduler-only test.
                return {"status": "processed", "reason_code": "native_rate_limit"} if number == 1 else {"status": "processed"}
            return {"status": "processed"}

        with mock.patch("codex_mem.service._record_workers", side_effect=metadata):
            self.run_worker(processor, max_cycles=6)
        effective = [count[1] for count in counts]
        self.assertIn(1, effective)
        self.assertEqual(2, effective[-1])
        self.assertLessEqual(max(count[2] for count in counts), 2)

    def test_explicit_broad_retry_dispatches_only_one_call(self):
        enqueue(self.project, self.data_dir, retry_failed=True, clock=self.clock)
        calls = []
        self.run_worker(lambda *a, **k: calls.append(k) or {"status": "idle"}, max_cycles=1)
        self.assertEqual(1, len(calls))
        self.assertTrue(calls[0]["retry_failed"])

    def test_config_off_while_active_stops_before_next_cohort(self):
        enqueue(self.project, self.data_dir, clock=self.clock)
        barrier = threading.Barrier(2)
        calls = []
        def processor(project, **kwargs):
            calls.append(threading.get_ident())
            barrier.wait(timeout=5)
            if threading.current_thread().name.endswith("_0"):
                configure(self.data_dir, service_enabled=False)
            return {"status": "processed"}
        result = self.run_worker(processor, max_cycles=10)
        self.assertEqual("paused", result["status"])
        self.assertEqual(2, len(calls))
        self.assertIsNone(self.state()["projects"][str(self.project)]["inflight_generation"])

    def test_nonretryable_blocker_keeps_its_provenance_in_both_completion_orders(self):
        from codex_mem.service import _claim_due_project, _finish_cohort
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                self.data_dir = self.root / ("reverse" if reverse else "forward")
                configure(self.data_dir, capture_scope="selected", included_projects=[self.project], semantic_enabled=False)
                self.seed("auth")
                self.seed("rate")
                enqueue(self.project, self.data_dir, clock=self.clock)
                choice = _claim_due_project(self.data_dir, self.now, 30)
                results = []
                for reason in ("native_auth", "native_rate_limit"):
                    with Store(self.data_dir) as store:
                        job = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                                                            parallel_sessions=True)
                        store.fail_observation_batch(self.project, job["job_id"], job["lease_token"],
                                                     code="runner_failure", reason_code=reason)
                        results.append({"status": "failed", "code": "runner_failure", "reason_code": reason,
                                        "job_id": job["job_id"]})
                auth_job, rate_job = results[0]["job_id"], results[1]["job_id"]
                if reverse:
                    results.reverse()
                _finish_cohort(self.data_dir, str(self.project), choice["generation"], choice, results,
                    data_dir=self.data_dir, semantic_enabled=False, indexer=None, now=self.now,
                    max_timeout_retries=2, retry_backoff=5)
                before = self.state()["projects"][str(self.project)]
                self.assertTrue(before["blocked"])
                self.assertEqual(auth_job, before["last_failure_job"])
                self.assertEqual("native_auth", before["last_failure_detail"]["reason_code"])
                self.assertEqual(rate_job, before["retry_job_id"])
                processor = mock.Mock(return_value={"status": "idle"})
                self.run_worker(processor, max_cycles=1)
                processor.assert_not_called()
                after = self.state()["projects"][str(self.project)]
                self.assertTrue(after["blocked"])
                self.assertEqual(auth_job, after["last_failure_job"])
                self.assertEqual(rate_job, after["retry_job_id"])
                self.assertEqual([], after["pending_retries"])
                # Repairing the blocker must preserve the other lane's exact
                # automatic retry, including when the service now uses one worker.
                from codex_mem.service import recover_runner_failure
                self.assertEqual("queued", recover_runner_failure(self.project, self.data_dir,
                                 job_id=auth_job, clock=self.clock)["status"])
                recovered = []
                def repair(project, **kwargs):
                    with Store(self.data_dir) as store:
                        job = self.claim(store, project, kwargs)
                        if job is None:
                            return {"status": "idle"}
                        recovered.append(job["job_id"])
                        self.assertFalse(kwargs["retry_failed"])
                        store.finish_observation_batch(project, job["job_id"], job["lease_token"], disposition="skipped")
                        return {"status": "skipped", "job_id": job["job_id"]}
                self.run_worker(repair, processor_workers=1, max_cycles=6)
                self.assertEqual([auth_job, rate_job], recovered)

    def test_same_session_never_has_two_active_claims(self):
        self.seed("same")
        self.seed("same")
        enqueue(self.project, self.data_dir, clock=self.clock)
        second_checked = threading.Event()
        jobs = []
        def processor(project, **kwargs):
            with Store(self.data_dir) as store:
                job = store.claim_observation_batch(project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                    max_entries=1, parallel_sessions=kwargs["parallel_sessions"])
                if job is None:
                    second_checked.set()
                    return {"status": "idle"}
                jobs.append(job["job_id"])
                self.assertTrue(second_checked.wait(timeout=5))
                self.assertEqual(1, store.status(project)["observation_jobs"]["running"])
                store.finish_observation_batch(project, job["job_id"], job["lease_token"], disposition="skipped")
                return {"status": "skipped", "job_id": job["job_id"]}
        result = self.run_worker(processor, max_cycles=3)
        self.assertEqual("cycle_limit", result["status"])
        self.assertEqual(2, len(set(jobs)))
        self.assertEqual({}, self.state()["projects"])

    def test_stale_cohort_and_expired_store_lease_recover_on_restart(self):
        self.seed("crashed")
        enqueue(self.project, self.data_dir, clock=self.clock)
        with Store(self.data_dir) as store:
            old = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                                                parallel_sessions=True)
        import sqlite3
        with closing(sqlite3.connect(self.data_dir / "memory.sqlite3")) as connection:
            connection.execute("UPDATE observation_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                               (old["job_id"],))
            connection.commit()
        state = self.state()
        record = state["projects"][str(self.project)]
        record["inflight_generation"] = record["generation"]
        record["inflight_until"] = self.now - 1
        (self.data_dir / SERVICE_STATE_FILENAME).write_text(json.dumps(state))
        recovered = []
        def processor(project, **kwargs):
            with Store(self.data_dir) as store:
                job = self.claim(store, project, kwargs)
                if job is None:
                    return {"status": "idle"}
                recovered.append(job["job_id"])
                store.finish_observation_batch(project, job["job_id"], job["lease_token"], disposition="skipped")
                return {"status": "skipped", "job_id": job["job_id"]}
        result = self.run_worker(processor, max_cycles=2)
        self.assertEqual("cycle_limit", result["status"])
        self.assertEqual([old["job_id"]], recovered)
        self.assertEqual({}, self.state()["projects"])

    def test_injected_processor_defaults_to_serial_and_worker_limits_are_checked(self):
        enqueue(self.project, self.data_dir, clock=self.clock)
        calls = []
        run_service(self.data_dir, processor=lambda *a, **k: calls.append(k) or {"status": "idle"}, max_cycles=1)
        self.assertEqual(1, len(calls))
        self.assertNotIn("parallel_sessions", calls[0])
        for invalid in (0, 5, True, 1.5):
            with self.assertRaises(ValueError):
                self.run_worker(lambda *a, **k: {"status": "idle"}, processor_workers=invalid, max_cycles=1)


if __name__ == "__main__":
    unittest.main()
