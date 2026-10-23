"""Storage diagnostics and bounded recovery through the processor/service seams."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem.processor import process_pending
from codex_mem.service import SERVICE_STATE_FILENAME, enqueue, run_service, service_status
from codex_mem.store import Store, StoreError
from codex_mem.observation_diagnostics import safe_storage_failure_detail


# SQLite result codes are stable; Python 3.10 does not export these names.
SQLITE_BUSY = getattr(sqlite3, "SQLITE_BUSY", 5)
SQLITE_CORRUPT = getattr(sqlite3, "SQLITE_CORRUPT", 11)


def sqlite_failure(code):
    error = sqlite3.OperationalError("database is busy; private SQL and secret-access-token")
    error.sqlite_errorcode = code
    return error


def storage_failure(*, safe=True, code=SQLITE_BUSY):
    error = StoreError("private SQL and secret-access-token")
    error.reason_code = "sqlite_busy" if code == SQLITE_BUSY else "sqlite_corrupt"
    error.sqlite_errorcode = code
    error.os_errno = None
    error.retry_safe = safe
    return error


class StorageResilienceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = (self.root / "project").resolve()
        self.project.mkdir()
        self.data_dir = self.root / "memory"
        configure(self.data_dir, capture_scope="selected", included_projects=[self.project], semantic_enabled=False)
        self.now = 1000.0

    def clock(self):
        return self.now

    def sleep(self, delay):
        self.now += delay

    def state(self):
        return json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())["projects"][str(self.project)]

    def test_real_sqlite_begin_contention_is_safe_but_does_not_invoke_operation(self):
        with Store(self.data_dir) as store:
            blocker = sqlite3.connect(store.db_path, isolation_level=None)
            self.addCleanup(blocker.close)
            blocker.execute("BEGIN IMMEDIATE")
            store._connection.execute("PRAGMA busy_timeout=0")
            operation = mock.Mock()
            with mock.patch("codex_mem.store.time.sleep"), self.assertRaises(StoreError) as raised:
                store._write(operation)
            self.assertEqual("sqlite_busy", getattr(raised.exception, "reason_code", None))
            self.assertTrue(getattr(raised.exception, "retry_safe", False))
            self.assertEqual(SQLITE_BUSY, raised.exception.sqlite_errorcode)
            operation.assert_not_called()
            blocker.execute("ROLLBACK")

    def test_commit_failure_never_replays_operation_or_marks_retry_safe(self):
        with Store(self.data_dir) as store:
            connection = store._connection
            proxy = mock.Mock(wraps=connection)

            def execute(sql):
                if sql == "COMMIT":
                    raise sqlite_failure(SQLITE_BUSY)
                return connection.execute(sql)

            proxy.execute.side_effect = execute
            proxy.in_transaction = True
            store._conn = proxy
            operation = mock.Mock()
            with mock.patch("codex_mem.store.time.sleep"), self.assertRaises(StoreError) as raised:
                store._write(operation)
            self.assertEqual(1, operation.call_count)
            self.assertFalse(getattr(raised.exception, "retry_safe", False))
            self.assertNotIn("secret-access-token", str(raised.exception))
            store._conn = connection

    def test_inner_store_error_cannot_grant_outer_transaction_retry(self):
        with Store(self.data_dir) as store:
            with self.assertRaises(StoreError) as raised:
                store._write(lambda: (_ for _ in ()).throw(storage_failure()))
        self.assertFalse(raised.exception.retry_safe)

    def test_preclaim_error_retains_allowlisted_diagnostic_and_stage(self):
        with mock.patch("codex_mem.processor.Store", side_effect=storage_failure()):
            result = process_pending(self.project, self.data_dir)
        detail = result.get("failure_detail", {})
        self.assertEqual("storage", detail.get("stage"))
        self.assertEqual("open", detail.get("operation"))
        self.assertEqual("sqlite_busy", detail.get("reason_code"))
        self.assertTrue(detail.get("retry_safe"))
        self.assertNotIn("secret-access-token", json.dumps(result))

    def test_real_claim_contention_has_no_job_and_keeps_the_claim_stage(self):
        with Store(self.data_dir) as store:
            store.remember(self.project, "Raw", "Observation", source="hook:Stop", session_id="session")
        blocker = sqlite3.connect(self.data_dir / "memory.sqlite3", isolation_level=None)
        self.addCleanup(blocker.close)
        original = Store.claim_observation_batch

        def blocked_claim(store, *args, **kwargs):
            blocker.execute("BEGIN IMMEDIATE")
            store._connection.execute("PRAGMA busy_timeout=0")
            return original(store, *args, **kwargs)

        with mock.patch.object(Store, "claim_observation_batch", blocked_claim), mock.patch("codex_mem.store.time.sleep"):
            result = process_pending(self.project, self.data_dir)
        blocker.execute("ROLLBACK")
        self.assertNotIn("job_id", result)
        self.assertEqual("claim", result["failure_detail"]["operation"])
        self.assertTrue(result["failure_detail"]["retry_safe"])
        with Store(self.data_dir) as store:
            self.assertEqual(0, store._connection.execute("SELECT count(*) FROM observation_jobs").fetchone()[0])

    def test_postclaim_storage_failure_keeps_job_and_forbids_automatic_retry(self):
        from tests.test_processor import ProcessorTests
        with Store(self.data_dir) as store:
            store.remember(self.project, "Raw", "Observation", source="hook:Stop", session_id="session")
        with mock.patch.object(Store, "finish_observation_batch", side_effect=storage_failure()):
            result = process_pending(self.project, self.data_dir, runner=lambda _: ProcessorTests.receipt({"disposition": "skipped", "notes": []}))
        self.assertIn("job_id", result)
        self.assertEqual("process", result["failure_detail"]["operation"])
        self.assertFalse(result["failure_detail"]["retry_safe"])

    def test_usage_receipt_write_failure_keeps_committed_success(self):
        from tests.test_processor import ProcessorTests
        with Store(self.data_dir) as store:
            store.remember(self.project, "Raw", "Observation", source="hook:Stop", session_id="session")
        with mock.patch("codex_mem.observer_usage_store.finish_attempt", side_effect=storage_failure()):
            result = process_pending(self.project, self.data_dir, runner=lambda _: ProcessorTests.receipt({"disposition": "skipped", "notes": []}))
        self.assertEqual("skipped", result["status"])
        self.assertFalse(result["observer_usage_recorded"])
        self.assertFalse(result["observer_usage_failure_detail"]["retry_safe"])
        self.assertEqual("sqlite_busy", result["observer_usage_failure_detail"]["reason_code"])
        self.assertNotIn("secret-access-token", json.dumps(result))

    def test_invalid_metadata_cannot_grant_retry_or_persist_private_text(self):
        self.assertIsNone(safe_storage_failure_detail({"stage": "storage", "code": "storage_failure", "operation": ["private"]}))
        checked = safe_storage_failure_detail({"stage": "storage", "code": "storage_failure", "operation": "claim",
            "reason_code": "sqlite_busy", "sqlite_errorcode": SQLITE_CORRUPT, "retry_safe": True,
            "os_errno": "secret-access-token", "private": "secret-access-token"})
        self.assertFalse(checked["retry_safe"])
        self.assertNotIn("secret-access-token", json.dumps(checked))

    def test_parallel_cohort_uses_the_same_bounded_preclaim_policy(self):
        enqueue(self.project, self.data_dir, clock=self.clock)
        receipt = {"status": "failed", "code": "storage_failure", "failure_detail": {
            "stage": "storage", "code": "storage_failure", "operation": "claim", "reason_code": "sqlite_busy",
            "sqlite_errorcode": SQLITE_BUSY, "os_errno": None, "retry_safe": True}}
        for attempt in range(3):
            run_service(self.data_dir, processor=lambda *a, **k: receipt, processor_workers=2,
                        clock=self.clock, sleeper=self.sleep, max_cycles=1)
            record = self.state()
            self.assertEqual(attempt >= 2, record["blocked"])
            self.assertEqual("claim", record["last_failure_detail"]["operation"])
            self.now = record["due_at"]

    def test_bounded_preclaim_retry_keeps_backoff_across_enqueue_and_restart(self):
        enqueue(self.project, self.data_dir, clock=self.clock)

        def processor(project, **kwargs):
            with mock.patch("codex_mem.processor.Store", side_effect=storage_failure()):
                return process_pending(project, **kwargs)

        for attempt, delay in ((1, 5), (2, 10)):
            run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep, max_cycles=1)
            record = self.state()
            self.assertFalse(record["blocked"])
            self.assertEqual(attempt, record["attempts"])
            self.assertEqual(self.now + delay, record["due_at"])
            self.now += 1
            enqueue(self.project, self.data_dir, clock=self.clock)
            self.assertEqual(record["due_at"], self.state()["due_at"])
            self.now = record["due_at"]
        run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep, max_cycles=1)
        self.assertTrue(self.state()["blocked"])
        self.assertEqual("sqlite_busy", self.state()["last_failure_detail"]["reason_code"])
        persisted = (self.data_dir / SERVICE_STATE_FILENAME).read_text()
        self.assertNotIn("secret-access-token", persisted)
        self.assertEqual(1, service_status(self.data_dir, clock=self.clock)["blocked_projects"])

    def test_retry_succeeds_without_broad_failed_batch_authorization(self):
        enqueue(self.project, self.data_dir, clock=self.clock)
        calls = []

        def processor(project, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                with mock.patch("codex_mem.processor.Store", side_effect=storage_failure()):
                    return process_pending(project, **kwargs)
            return process_pending(project, **kwargs)

        run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep, max_cycles=10)
        self.assertEqual(2, len(calls))
        self.assertTrue(all(not call["retry_failed"] and call.get("retry_job_id") is None for call in calls))
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["blocked_projects"])

    def assert_preclaim_permission_preserved(self, workers, *, explicit=False):
        from codex_mem.processor import MODEL, PROCESSOR_ID, REASONING_EFFORT
        with Store(self.data_dir) as store:
            store.remember(self.project, "Tool", "Verified output", source="hook:PostToolUse", session_id="session")
            job = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            store.fail_observation_batch(self.project, job["job_id"], job["lease_token"],
                                         code="runner_failure", reason_code="jev_filter_transport")
        enqueue(self.project, self.data_dir, retry_failed=explicit, clock=self.clock)
        if not explicit:
            state = json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())
            state["projects"][str(self.project)].update(retry_job_id=job["job_id"], retry_error_code="runner_failure",
                                                         retry_attempt_count=1, attempts=3)
            (self.data_dir / SERVICE_STATE_FILENAME).write_text(json.dumps(state))
        calls = []
        def processor(project, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                with mock.patch("codex_mem.processor.Store", side_effect=storage_failure()):
                    return process_pending(project, **kwargs)
            with Store(self.data_dir) as store:
                selected = store.claim_observation_batch(project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                    retry_failed=kwargs["retry_failed"], retry_job_id=kwargs.get("retry_job_id"),
                    retry_error_code=kwargs.get("retry_error_code"), retry_attempt_count=kwargs.get("retry_attempt_count"))
                self.assertEqual(job["job_id"], selected["job_id"])
                store.finish_observation_batch(project, selected["job_id"], selected["lease_token"], disposition="skipped")
            return {"status": "skipped", "job_id": selected["job_id"]}
        run_service(self.data_dir, processor=processor, processor_workers=workers, clock=self.clock,
                    sleeper=self.sleep, max_cycles=1)
        record = self.state()
        self.assertFalse(record["blocked"])
        self.assertEqual(self.now + 5, record["due_at"])
        if explicit:
            self.assertTrue(record["retry_requested"])
        else:
            self.assertEqual((job["job_id"], "runner_failure", 1, 3),
                             (record["retry_job_id"], record["retry_error_code"], record["retry_attempt_count"], record["attempts"]))
            self.assertEqual([], record["pending_retries"])
        self.now = record["due_at"]
        run_service(self.data_dir, processor=processor, processor_workers=workers, clock=self.clock,
                    sleeper=self.sleep, max_cycles=1)
        self.assertEqual(calls[0]["retry_failed"], calls[1]["retry_failed"])
        self.assertEqual(calls[0].get("retry_job_id"), calls[1].get("retry_job_id"))
        self.assertEqual(calls[0].get("retry_error_code"), calls[1].get("retry_error_code"))
        self.assertEqual(calls[0].get("retry_attempt_count"), calls[1].get("retry_attempt_count"))

    def test_single_preclaim_busy_preserves_exact_retry(self):
        self.assert_preclaim_permission_preserved(1)

    def test_cohort_preclaim_busy_preserves_exact_retry(self):
        self.assert_preclaim_permission_preserved(2)

    def test_single_preclaim_busy_preserves_unconsumed_explicit_retry(self):
        self.assert_preclaim_permission_preserved(1, explicit=True)

    def test_cohort_preclaim_busy_preserves_unconsumed_explicit_retry(self):
        self.assert_preclaim_permission_preserved(2, explicit=True)

    def assert_exact_storage_retries_exhaust_without_losing_permission(self, workers, *, explicit=False):
        from codex_mem.processor import MODEL, PROCESSOR_ID, REASONING_EFFORT
        with Store(self.data_dir) as store:
            store.remember(self.project, "Tool", "Original output", source="hook:PostToolUse", session_id="session")
            job = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            store.fail_observation_batch(self.project, job["job_id"], job["lease_token"],
                                         code="runner_failure", reason_code="jev_filter_transport")
        enqueue(self.project, self.data_dir, clock=self.clock, retry_failed=explicit)
        state = json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())
        if not explicit:
            state["projects"][str(self.project)].update(retry_job_id=job["job_id"], retry_error_code="runner_failure",
                                                         retry_attempt_count=1, attempts=3)
        (self.data_dir / SERVICE_STATE_FILENAME).write_text(json.dumps(state))
        def busy(project, **kwargs):
            with mock.patch("codex_mem.processor.Store", side_effect=storage_failure()):
                return process_pending(project, **kwargs)
        for attempt in range(1, 4):
            run_service(self.data_dir, processor=busy, processor_workers=workers, clock=self.clock,
                        sleeper=self.sleep, max_cycles=1)
            record = self.state()
            self.assertEqual(attempt == 3, record["blocked"])
            self.assertEqual(attempt, record["storage_attempts"])
            expected = (None, None, None, 0) if explicit else (job["job_id"], "runner_failure", 1, 3)
            self.assertEqual(expected, (record["retry_job_id"], record["retry_error_code"],
                                        record["retry_attempt_count"], record["attempts"]))
            self.assertEqual(explicit, record["retry_requested"])
            self.assertEqual([], record["pending_retries"])
            self.now = record["due_at"]
        with Store(self.data_dir) as store:
            self.assertEqual(1, store.observation_job_status(self.project, job["job_id"])["attempt_count"])

    def test_single_storage_exhaustion_preserves_exact_permission(self):
        self.assert_exact_storage_retries_exhaust_without_losing_permission(1)

    def test_cohort_storage_exhaustion_preserves_exact_permission(self):
        self.assert_exact_storage_retries_exhaust_without_losing_permission(2)

    def test_single_storage_exhaustion_preserves_explicit_permission(self):
        self.assert_exact_storage_retries_exhaust_without_losing_permission(1, explicit=True)

    def test_cohort_storage_exhaustion_preserves_explicit_permission(self):
        self.assert_exact_storage_retries_exhaust_without_losing_permission(2, explicit=True)

    def test_unknown_corrupt_and_postclaim_errors_remain_blocked(self):
        for error, job_id in ((StoreError("private SQL"), None),
                              (storage_failure(code=SQLITE_CORRUPT), None),
                              (storage_failure(safe=False), None),
                              (storage_failure(), "uncertain-job")):
            with self.subTest(error=type(error), job_id=job_id):
                enqueue(self.project, self.data_dir, retry_failed=True, clock=self.clock)

                def processor(project, **kwargs):
                    with mock.patch("codex_mem.processor.Store", side_effect=error):
                        result = process_pending(project, **kwargs)
                    if job_id:
                        result["job_id"] = job_id
                    return result

                run_service(self.data_dir, processor=processor, clock=self.clock, sleeper=self.sleep, max_cycles=1)
                self.assertTrue(self.state()["blocked"])


if __name__ == "__main__":
    unittest.main()
