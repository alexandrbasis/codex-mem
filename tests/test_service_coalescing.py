from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import json
from unittest import mock

from codex_mem.config import configure
from codex_mem.processor import MODEL, PROCESSOR_ID, REASONING_EFFORT
from codex_mem.service import SERVICE_STATE_FILENAME, _claim_due_project, enqueue, run_service
from codex_mem.store import Store


class CaptureCoalescingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.data = self.root / 'memory'
        configure(self.data, capture_scope='selected', included_projects=[self.project], semantic_enabled=False)
        self.now = 1000.0

    def enqueue(self, delay=5):
        return enqueue(self.project, self.data, clock=lambda: self.now, coalesce_seconds=delay)

    def choice(self):
        return _claim_due_project(self.data, self.now, 30)

    def test_burst_waits_for_first_deadline_without_sliding(self):
        self.enqueue()
        self.assertEqual('wait', self.choice()['kind'])
        for second in range(1, 5):
            self.now = 1000 + second
            self.enqueue()
            self.assertEqual(1005, self.choice()['due_at'])
        self.now = 1005
        self.assertEqual('work', self.choice()['kind'])

    def test_immediate_boundary_flushes_waiting_capture(self):
        self.enqueue()
        self.now += 1
        self.enqueue(delay=0)
        self.assertEqual('work', self.choice()['kind'])

    def test_explicit_write_is_not_delayed_by_later_capture(self):
        self.enqueue(delay=0)
        self.enqueue()
        self.assertEqual('work', self.choice()['kind'])

    def test_coalescing_does_not_starve_ready_project(self):
        other = self.root / 'other'
        other.mkdir()
        configure(self.data, capture_scope='selected', included_projects=[self.project, other])
        self.enqueue()
        enqueue(other, self.data, clock=lambda: self.now)
        self.assertEqual(str(other.resolve()), self.choice()['project'])

    def assert_hook_flush(self, boundary, **extra):
        from codex_mem.hooks import handle_hook
        payload = {"cwd": str(self.project), "session_id": "session", "turn_id": "turn"}
        real_enqueue = enqueue

        def fixed_enqueue(*args, **kwargs):
            return real_enqueue(*args, clock=lambda: self.now, **kwargs)

        with Store(self.data) as store, mock.patch("codex_mem.service.enqueue", side_effect=fixed_enqueue), mock.patch(
                "codex_mem.service.start_service", return_value={"status": "test-disabled"}):
            handle_hook({**payload, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                         "tool_use_id": "tool", "tool_input": {"command": "pytest"},
                         "tool_response": {"output": "Tests passed", "exit_code": 0}}, store)
            self.assertEqual("wait", self.choice()["kind"])
            self.now += 1
            handle_hook({**payload, "hook_event_name": boundary, **extra}, store)
            self.assertEqual("work", self.choice()["kind"])

    def test_stop_hook_flushes_actual_tool_capture(self):
        self.assert_hook_flush("Stop", last_assistant_message="Verified tests passed.")

    def test_empty_stop_flushes_actual_tool_capture(self):
        self.assert_hook_flush("Stop")

    def test_semantic_only_capture_is_not_delayed(self):
        from codex_mem.integration import after_write
        configure(self.data, processor_enabled=False, semantic_enabled=True)
        real_enqueue = enqueue
        def fixed_enqueue(*args, **kwargs):
            return real_enqueue(*args, clock=lambda: self.now, **kwargs)
        with mock.patch("codex_mem.service.enqueue", side_effect=fixed_enqueue), mock.patch(
                "codex_mem.service.start_service", return_value={"status": "test-disabled"}):
            after_write(self.project, self.data, coalesce_seconds=5)
        self.assertEqual("work", self.choice()["kind"])

    def test_precompact_hook_flushes_actual_tool_capture(self):
        self.assert_hook_flush("PreCompact", trigger="auto")

    def test_restart_keeps_fixed_deadline(self):
        self.enqueue()
        self.now += 2
        self.enqueue()
        self.assertEqual(1005, self.choice()["due_at"])

    def test_inflight_capture_keeps_deadline_after_success(self):
        from codex_mem.service import _finish_success
        self.enqueue(delay=0)
        choice = self.choice()
        self.now += 1
        self.enqueue()
        _finish_success(self.data.resolve(), str(self.project.resolve()), choice["generation"],
                        "skipped", False, self.now)
        self.assertEqual(1006, self.choice()["due_at"])

    def test_inflight_capture_keeps_deadline_after_cohort(self):
        from codex_mem.service import _finish_cohort
        self.enqueue(delay=0)
        choice = self.choice()
        self.now += 1
        self.enqueue()
        _finish_cohort(self.data.resolve(), str(self.project.resolve()), choice["generation"], choice,
                       [{"status": "skipped"}], now=self.now, retry_backoff=5,
                       max_timeout_retries=2, semantic_enabled=False,
                       indexer=lambda *a, **k: None, data_dir=self.data)
        self.assertEqual(1006, self.choice()["due_at"])

    def test_repaired_rejection_does_not_leave_parallel_queue_parked(self):
        from codex_mem.service import _finish_cohort
        self.enqueue(delay=0)
        choice = self.choice()
        with Store(self.data) as store:
            store.remember(self.project, "Tool", "Verified output", source="hook:PostToolUse",
                           session_id="session", turn_id="turn")
            job = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            store.fail_observation_batch(self.project, job["job_id"], job["lease_token"], code="invalid_response", reason_code="invalid_json")
        def finish(choice, result):
            return _finish_cohort(self.data.resolve(), str(self.project.resolve()), choice["generation"], choice,
                                  [result], now=self.now, retry_backoff=5, max_timeout_retries=2,
                                  semantic_enabled=False, indexer=None, data_dir=self.data)
        finish(choice, {"status": "failed", "code": "invalid_response", "reason_code": "invalid_json", "job_id": job["job_id"]})
        with Store(self.data) as store:
            repaired = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                                                      retry_failed=True)
            store.finish_observation_batch(self.project, repaired["job_id"], repaired["lease_token"],
                                           disposition="skipped")
        self.enqueue(delay=0)
        finish(self.choice(), {"status": "idle"})
        state = json.loads((self.data / SERVICE_STATE_FILENAME).read_text())
        self.assertNotIn(str(self.project.resolve()), state["projects"])

    def replay(self, delay, capture_count=5):
        calls = []
        captured = 0

        def capture():
            nonlocal captured
            with Store(self.data) as store:
                store.remember(self.project, 'Tool', 'Verified output ' + str(captured),
                               source='hook:PostToolUse', session_id='session', turn_id='turn')
            captured += 1
            self.enqueue(delay)

        def sleep(seconds):
            self.now += seconds
            if captured < capture_count:
                capture()

        def processor(project, data_dir=None, **kwargs):
            with Store(data_dir) as store:
                job = store.claim_observation_batch(project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
                if job is None:
                    return {'status': 'idle'}
                calls.append((self.now, len(job['sources'])))
                store.finish_observation_batch(project, job['job_id'], job['lease_token'], disposition='skipped')
            return {'status': 'skipped'}

        capture()
        run_service(self.data, processor=processor, processor_workers=1, clock=lambda: self.now,
                    sleeper=sleep, poll_interval=1, max_cycles=40)
        return calls

    def test_real_store_burst_uses_one_batch(self):
        calls = self.replay(5)
        self.assertEqual([(1005, 5)], calls)

    def test_immediate_baseline_uses_five_batches(self):
        calls = self.replay(0)
        self.assertEqual(5, len(calls))
        self.assertEqual([1] * 5, [count for _, count in calls])

    def test_sustained_stream_dispatches_without_waiting_for_quiet(self):
        calls = self.replay(5, capture_count=15)
        self.assertEqual([(1005, 6), (1011, 6), (1017, 3)], calls)

    def test_invalid_coalescing_delay_rejected(self):
        for delay in (-1, 6, float("inf"), float("nan"), True, "5"):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                self.enqueue(delay=delay)
