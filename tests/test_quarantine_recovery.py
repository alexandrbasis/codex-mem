"""Coordinator recovery permissions bind exact inspected jobs without model calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem import quarantine_recovery as recovery
from codex_mem.processor import MODEL, PROCESSOR_ID, REASONING_EFFORT
from codex_mem.store import Store


class QuarantineRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.base = self.root / "memory"
        configure(self.base, capture_scope="selected", included_projects=[self.project])
        self.jobs = []
        for index in range(6):
            with Store(self.base) as store:
                store.remember(self.project, str(index), "synthetic evidence", source="hook:PostToolUse", session_id=str(index))
                job = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
                store.fail_observation_batch(self.project, job["job_id"], job["lease_token"], "invalid_response")
                fingerprint = store._connection.execute("SELECT input_fingerprint FROM observation_jobs WHERE id=?", (job["job_id"],)).fetchone()[0]
            self.jobs.append(dict(job_id=job["job_id"], error_code="invalid_response", attempt_count=1, input_fingerprint=fingerprint))

    def schedule(self, index=0, **changes):
        return recovery.schedule(self.project, self.base, **dict(self.jobs[index], **changes))

    def test_schedule_is_idempotent_and_dispatch_owns_exact_selectors(self):
        processor = mock.Mock(return_value={"status": "processed", "private": "must not persist"})
        with mock.patch.object(recovery, "process_pending", processor):
            self.assertEqual("queued", self.schedule()["status"])
            self.assertEqual("already_scheduled", self.schedule()["status"])
            processor.assert_not_called()
            result = recovery.run_next(self.project, self.base)
            self.assertEqual("processed", result["permission"]["outcome"])
            self.assertIsNone(recovery.run_next(self.project, self.base))
        self.assertEqual(1, processor.call_count)
        kwargs = processor.call_args.kwargs
        self.assertEqual(self.jobs[0]["job_id"], kwargs["retry_job_id"])
        self.assertEqual(1, kwargs["retry_attempt_count"])
        self.assertEqual(self.jobs[0]["input_fingerprint"], kwargs["retry_input_fingerprint"])
        self.assertFalse(kwargs["retry_failed"])
        self.assertTrue(kwargs["retry_one_shot"])
        text = (self.base / recovery.STATE_FILENAME).read_text()
        self.assertNotIn("must not persist", text)
        self.assertEqual(0o600, (self.base / recovery.STATE_FILENAME).stat().st_mode & 0o777)
        self.assertEqual("already_scheduled", self.schedule()["status"])

    def test_stale_attempt_fingerprint_and_foreign_job_refuse_dispatch(self):
        for changes in ({"attempt_count": 2}, {"input_fingerprint": "0" * 64}, {"job_id": "missing"}):
            self.assertEqual("recovery_unavailable", self.schedule(**changes)["code"])
        self.schedule()
        with Store(self.base) as store:
            store._connection.execute("UPDATE observation_jobs SET attempt_count=2 WHERE id=?", (self.jobs[0]["job_id"],))
        processor = mock.Mock()
        result = recovery.run_next(self.project, self.base, processor=processor)
        self.assertEqual("unavailable", result["permission"]["outcome"])
        processor.assert_not_called()
        self.assertIsNone(recovery.run_next(self.project, self.base, processor=processor))

    def test_failed_and_interrupted_attempts_do_not_replay_permission(self):
        self.schedule()
        processor = mock.Mock(return_value={"status": "failed", "code": "timeout"})
        self.assertEqual("failed", recovery.run_next(self.project, self.base, processor=processor)["permission"]["outcome"])
        self.assertIsNone(recovery.run_next(self.project, self.base, processor=processor))
        self.schedule(1)
        def crash(*args, **kwargs):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            recovery.run_next(self.project, self.base, processor=crash)
        self.assertEqual("dispatching", recovery.status(self.base)[1]["state"])
        self.assertIsNone(recovery.run_next(self.project, self.base, processor=processor))
        self.assertEqual(1, processor.call_count)

    def test_manifest_exceeds_worker_width_without_losing_permissions(self):
        for index in range(6):
            self.assertEqual("queued", self.schedule(index)["status"])
        processor = mock.Mock(return_value={"status": "skipped"})
        for index in range(6):
            result = recovery.run_next(self.project, self.base, processor=processor)
            self.assertEqual(self.jobs[index]["job_id"], result["permission"]["job_id"])
        self.assertIsNone(recovery.run_next(self.project, self.base, processor=processor))
        self.assertEqual(6, processor.call_count)

    def test_changed_scope_and_legacy_profile_never_dispatch(self):
        with Store(self.base) as store:
            store._connection.execute("UPDATE observation_jobs SET model='gpt-5.6-luna' WHERE id=?", (self.jobs[0]["job_id"],))
        self.assertEqual("unsupported_profile", self.schedule()["code"])
        self.schedule(1)
        configure(self.base, processor_enabled=False)
        processor = mock.Mock()
        result = recovery.run_next(self.project, self.base, processor=processor)
        self.assertEqual("disabled", result["permission"]["outcome"])
        processor.assert_not_called()

    def test_malformed_and_symlinked_permission_files_fail_closed(self):
        self.schedule()
        target = self.base / recovery.STATE_FILENAME
        state = json.loads(target.read_text())
        state["permissions"][0]["attempt_count"] = True
        target.write_text(json.dumps(state))
        with self.assertRaises(recovery.RecoveryStateError):
            recovery.run_next(self.project, self.base, processor=mock.Mock())
        target.unlink()
        target.symlink_to(self.root / "elsewhere")
        with self.assertRaises(recovery.RecoveryStateError):
            self.schedule()

    def test_caller_cannot_widen_selector(self):
        self.schedule()
        with self.assertRaises(ValueError):
            recovery.run_next(self.project, self.base, retry_failed=True)
        self.assertEqual("scheduled", recovery.status(self.base)[0]["state"])

    def test_legacy_upgrade_requires_explicit_permission_and_preserves_receipt(self):
        with Store(self.base) as store:
            store._connection.execute("UPDATE observation_jobs SET model='gpt-5.6-luna' WHERE id=?", (self.jobs[0]["job_id"],))
        self.assertEqual("unsupported_profile", self.schedule()["code"])
        self.assertEqual("queued", self.schedule(allow_previous_profile=True)["status"])
        processor = mock.Mock(return_value={"status": "processed", "job_id": "successor"})
        result = recovery.run_next(self.project, self.base, processor=processor)
        self.assertTrue(processor.call_args.kwargs["retry_previous_profile"])
        self.assertEqual("successor", result["result"]["job_id"])
        self.assertEqual(self.jobs[0]["job_id"], result["result"]["parent_job_id"])
        self.assertEqual(self.jobs[0]["job_id"], result["permission"]["job_id"])

    def test_allowing_previous_profile_does_not_force_current_job_into_legacy_selector(self):
        self.assertEqual("queued", self.schedule(allow_previous_profile=True)["status"])
        processor = mock.Mock(return_value={"status": "skipped"})
        result = recovery.run_next(self.project, self.base, processor=processor)
        self.assertNotIn("retry_previous_profile", processor.call_args.kwargs)
        self.assertEqual("skipped", result["permission"]["outcome"])

    def test_existing_legacy_successor_refuses_new_authorization(self):
        with Store(self.base) as store:
            store._connection.execute("UPDATE observation_jobs SET model='gpt-5.6-luna' WHERE id=?", (self.jobs[0]["job_id"],))
            store._connection.execute("CREATE TABLE IF NOT EXISTS observation_job_recoveries (parent_job_id TEXT PRIMARY KEY, successor_job_id TEXT, project TEXT, created_at TEXT)")
            store._connection.execute("INSERT INTO observation_job_recoveries VALUES (?,?,?,?)", (self.jobs[0]["job_id"], self.jobs[1]["job_id"], str(self.project.resolve()), "synthetic"))
        self.assertEqual("recovery_unavailable", self.schedule(allow_previous_profile=True)["code"])

    def test_no_manifest_does_not_create_runtime_artifacts(self):
        absent = self.root / "absent"
        self.assertIsNone(recovery.run_next(self.project, absent))
        self.assertFalse(absent.exists())

    def test_fixed_diagnostics_survive_but_untrusted_text_is_removed(self):
        self.schedule()
        processor = mock.Mock(return_value={"status": "failed", "code": "invalid_response",
                                           "reason_code": "jev_quality_uncertain", "text": "private-marker"})
        result = recovery.run_next(self.project, self.base, processor=processor)["result"]
        self.assertTrue(result["recovery_one_shot"])
        self.assertEqual("invalid_response", result["code"])
        self.assertEqual("jev_quality_uncertain", result["reason_code"])
        self.assertNotIn("private-marker", json.dumps(result))
        self.schedule(1)
        processor.return_value = {"status": "failed", "code": "private-marker", "reason_code": "private-marker"}
        result = recovery.run_next(self.project, self.base, processor=processor)["result"]
        self.assertNotIn("code", result)
        self.assertNotIn("reason_code", result)

    def test_service_drains_multiple_permissions_and_fresh_work(self):
        from codex_mem import processor, service
        configure(self.base, semantic_enabled=False)
        for index in range(3):
            self.schedule(index)
        with Store(self.base) as store:
            store.remember(self.project, "fresh", "fresh synthetic evidence",
                           source="hook:PostToolUse", session_id="fresh")
        service.enqueue(self.project, self.base)
        calls = []

        def runner(request):
            return {"output": {"notes": [], "disposition": "skipped"},
                    "evidence": {"thread_start": {"thread_id": "worker", "model": MODEL,
                        "reasoning_effort": REASONING_EFFORT, "model_provider": "openai"},
                        "turn_started": {"thread_id": "worker", "turn_id": "turn"},
                        "turn_completed": True, "no_tools": True, "rerouted": False}}

        def execute(project, data_dir=None, **kwargs):
            calls.append(kwargs.get("retry_job_id"))
            if kwargs.get("retry_job_id") is not None:
                state = json.loads((self.base / service.SERVICE_STATE_FILENAME).read_text())
                self.assertEqual({"configured": 2, "effective": 2, "active": 1},
                                 state["owner"]["workers"])
            return processor.process_pending(project, data_dir, runner=runner, **kwargs)

        record_workers = service._record_workers
        with mock.patch.object(service, "_record_workers", wraps=record_workers) as records:
            service.run_service(self.base, processor=execute, processor_workers=2,
                                max_cycles=8, sleeper=lambda _: None)
        active_counts = [call.args[-1] for call in records.call_args_list]
        for index, active in enumerate(active_counts):
            if active == 1:
                self.assertEqual(0, active_counts[index + 1])
        self.assertEqual(["skipped"] * 3, [item["outcome"] for item in recovery.status(self.base)])
        for job in self.jobs[:3]:
            self.assertEqual(1, calls.count(job["job_id"]))
        self.assertIsNone(calls[1], "ordinary work must run between maintenance batches")
        with Store(self.base) as store:
            self.assertEqual(4, store._connection.execute(
                "SELECT COUNT(*) FROM observation_jobs WHERE status='skipped'").fetchone()[0])
            self.assertEqual(3, store._connection.execute(
                "SELECT COUNT(*) FROM observation_jobs WHERE status='failed'").fetchone()[0])

    def test_service_maintenance_timeout_does_not_schedule_automatic_replay(self):
        from codex_mem import processor, service
        configure(self.base, semantic_enabled=False)
        self.schedule()
        service.enqueue(self.project, self.base)
        runner = mock.Mock(side_effect=processor.ProcessorFailure("timeout"))
        calls = []

        def execute(project, data_dir=None, **kwargs):
            calls.append(kwargs.get("retry_job_id"))
            if kwargs.get("retry_job_id") is not None:
                state = json.loads((self.base / service.SERVICE_STATE_FILENAME).read_text())
                self.assertEqual(1, state["owner"]["workers"]["active"])
            return processor.process_pending(project, data_dir, runner=runner, **kwargs)

        with mock.patch.object(service, "_record_workers", wraps=service._record_workers) as records:
            service.run_service(self.base, processor=execute, processor_workers=1,
                                max_cycles=5, sleeper=lambda _: None)
        self.assertEqual([0, 1, 0], [call.args[-1] for call in records.call_args_list])
        self.assertEqual(1, runner.call_count)
        self.assertEqual(1, calls.count(self.jobs[0]["job_id"]))
        self.assertEqual("failed", recovery.status(self.base)[0]["outcome"])
        with Store(self.base) as store:
            status = store.observation_job_status(self.project, self.jobs[0]["job_id"])
            self.assertEqual(("failed", 2, "timeout"),
                             (status["status"], status["attempt_count"], status["error_code"]))

    def test_service_resets_maintenance_workers_after_dispatch_exception(self):
        from codex_mem import service
        configure(self.base, semantic_enabled=False)
        self.schedule()
        service.enqueue(self.project, self.base)

        def crash(project, data_dir=None, **kwargs):
            state = json.loads((self.base / service.SERVICE_STATE_FILENAME).read_text())
            self.assertEqual({"configured": 2, "effective": 2, "active": 1},
                             state["owner"]["workers"])
            raise RuntimeError("synthetic processor failure")

        with mock.patch.object(service, "_record_workers", wraps=service._record_workers) as records, \
                mock.patch.object(recovery, "run_next", side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, "synthetic processor failure"):
                service.run_service(self.base, processor=crash, processor_workers=2,
                                    max_cycles=2, sleeper=lambda _: None)
        self.assertEqual([0, 1, 0], [call.args[-1] for call in records.call_args_list])

    def test_busy_lane_preserves_permission_until_a_real_claim(self):
        from codex_mem.processor import process_pending
        configure(self.base, jev_filter_enabled=False)
        self.schedule()
        with Store(self.base) as store:
            store.remember(self.project, "active", "synthetic active evidence",
                           source="hook:Stop", session_id="active")
            active = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
        runner = mock.Mock(return_value={
            "output": {"notes": [], "disposition": "skipped"},
            "evidence": {"thread_start": {"thread_id": "worker", "model": MODEL,
                "reasoning_effort": REASONING_EFFORT, "model_provider": "openai"},
                "turn_started": {"thread_id": "worker", "turn_id": "turn"},
                "turn_completed": True, "no_tools": True, "rerouted": False}})

        def execute(project, data_dir=None, **kwargs):
            return process_pending(project, data_dir, runner=runner, **kwargs)

        deferred = recovery.run_next(self.project, self.base, processor=execute)
        self.assertEqual("scheduled", deferred["permission"]["state"])
        self.assertIsNone(deferred["permission"]["outcome"])
        self.assertEqual("deferred", deferred["result"]["status"])
        runner.assert_not_called()
        with Store(self.base) as store:
            self.assertEqual(1, store.observation_job_status(self.project, self.jobs[0]["job_id"])["attempt_count"])
            store.finish_observation_batch(self.project, active["job_id"], active["lease_token"], disposition="skipped")
        finished = recovery.run_next(self.project, self.base, processor=execute)
        self.assertEqual("complete", finished["permission"]["state"])
        self.assertEqual("skipped", finished["permission"]["outcome"])
        self.assertEqual(1, runner.call_count)

    def test_changed_attempt_never_rearms_a_misleading_no_claim_receipt(self):
        for index, returned in enumerate((
                {"status": "blocked", "code": "recovery_unavailable"},
                {"status": "deferred", "retry_at": 9999999999})):
            with self.subTest(status=returned["status"]):
                self.schedule(index)

                def execute(project, data_dir=None, **selectors):
                    with Store(data_dir) as store:
                        claimed = store.claim_observation_batch(project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                                                                **selectors)
                        self.assertIsNotNone(claimed)
                        store.fail_observation_batch(project, claimed["job_id"], claimed["lease_token"],
                                                     code="invalid_response")
                    return returned

                result = recovery.run_next(self.project, self.base, processor=execute)
                self.assertEqual("complete", result["permission"]["state"])
                self.assertEqual("unavailable", result["permission"]["outcome"])
                with Store(self.base) as store:
                    self.assertEqual(2, store.observation_job_status(self.project, self.jobs[index]["job_id"])["attempt_count"])
                self.assertIsNone(recovery.run_next(self.project, self.base, processor=execute))

    def test_legacy_successor_never_rearms_unchanged_parent_attempt(self):
        from codex_mem.store import _legacy_observation_fingerprint, project_key
        with Store(self.base) as store:
            source_ids = [row[0] for row in store._connection.execute(
                "SELECT source_id FROM observation_job_sources WHERE job_id=? ORDER BY source_id",
                (self.jobs[0]["job_id"],))]
            fingerprint = _legacy_observation_fingerprint(project_key(self.project), PROCESSOR_ID, source_ids)
            store._connection.execute("UPDATE observation_jobs SET model='gpt-5.6-luna',input_fingerprint=? WHERE id=?",
                                      (fingerprint, self.jobs[0]["job_id"]))
        self.schedule(allow_previous_profile=True, input_fingerprint=fingerprint)

        def execute(project, data_dir=None, **selectors):
            with Store(data_dir) as store:
                successor = store.claim_observation_batch(project, PROCESSOR_ID, MODEL, REASONING_EFFORT, **selectors)
                self.assertIsNotNone(successor)
                self.assertNotEqual(self.jobs[0]["job_id"], successor["job_id"])
            return {"status": "blocked", "code": "recovery_unavailable"}

        result = recovery.run_next(self.project, self.base, processor=execute)
        self.assertEqual("complete", result["permission"]["state"])
        self.assertEqual("unavailable", result["permission"]["outcome"])
        with Store(self.base) as store:
            parent = store.observation_job_status(self.project, self.jobs[0]["job_id"])
            self.assertEqual(1, parent["attempt_count"])
            self.assertIn("successor_job_id", parent)
        self.assertIsNone(recovery.run_next(self.project, self.base, processor=execute))
