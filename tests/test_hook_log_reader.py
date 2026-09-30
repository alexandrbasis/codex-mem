from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from codex_mem.cli import _doctor
from codex_mem.hook_log_reader import recent_hook_logs


class HookLogReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "memory"
        self.path = self.home / "logs" / "hooks.jsonl"

    def row(self, run_id, event, component="supervisor", **fields):
        return {"schema_version": 1, "timestamp": "2026-09-29T12:00:00+00:00",
                "run_id": run_id, "component": component, "event": event,
                "stage": "bootstrap", "pid": 123, "elapsed_ms": 1.5, **fields}

    def write(self, *rows, path=None):
        path = path or self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def test_absent_is_unknown_and_does_not_create_directory(self):
        result = recent_hook_logs(self.home)
        self.assertEqual("unknown", result["status"])
        self.assertEqual(0, result["sampled_runs"])
        self.assertFalse(self.home.exists())
        doctor = _doctor(str(self.home))
        self.assertEqual("UNKNOWN", doctor["hooks"]["status"])
        self.assertEqual(str(self.path), doctor["hook_logs"]["path"])
        self.assertFalse(self.home.exists())

    def test_failure_and_unfinished_are_distinct_and_private(self):
        good, bad, killed = (str(uuid.uuid4()) for _ in range(3))
        self.write(
            self.row(good, "started"), self.row(good, "completed", status="ok", error_count=0),
            self.row(bad, "error", component="worker", code="sqlite_failed",
                     error={"exception_type": "OperationalError", "sqlite_errorcode": 5,
                            "message": "secret", "frames": [{"file": "secret.py"}]},
                     payload="secret", message="secret", declared_hook_event="secret",
                     hook_event="secret", session_id="secret", turn_id="secret"),
            self.row(bad, "completed", status="degraded", error_count=1),
            self.row(killed, "started"),
        )
        result = recent_hook_logs(self.home)
        self.assertEqual("partial", result["status"])
        self.assertEqual(3, result["sampled_runs"])
        self.assertEqual(1, result["failures"])
        self.assertEqual(1, result["incomplete"])
        self.assertEqual(killed, result["recent_incomplete"][0]["run_id"])
        rendered = json.dumps(result)
        self.assertNotIn("secret", rendered)
        self.assertIn("sqlite_failed", rendered)

    def test_malformed_and_rotated_tail_are_partial_and_bounded(self):
        run_id = str(uuid.uuid4())
        self.write(self.row(run_id, "completed", status="failed", error_count=0),
                   path=Path(str(self.path) + ".1"))
        self.path.write_bytes(b"x" * 300000 + b"\n{bad json}\n")
        result = recent_hook_logs(self.home)
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["failures"])
        self.assertEqual(1, result["sampled_runs"])

    @unittest.skipUnless(hasattr(os, "mkfifo") and hasattr(os, "O_NOFOLLOW"), "requires POSIX")
    def test_symlink_and_fifo_are_not_opened_as_logs(self):
        self.path.parent.mkdir(parents=True)
        target = self.path.parent / "target"
        target.write_text("secret")
        self.path.symlink_to(target)
        self.assertEqual("partial", recent_hook_logs(self.home)["status"])
        self.path.unlink()
        os.mkfifo(self.path)
        self.assertEqual("partial", recent_hook_logs(self.home)["status"])

    def test_completed_cancelled_is_failure(self):
        run_id = str(uuid.uuid4())
        self.write(self.row(run_id, "completed", status="cancelled", error_count=0))
        result = recent_hook_logs(self.home)
        self.assertEqual("available", result["status"])
        self.assertEqual(1, result["failures"])
        self.assertEqual(0, result["incomplete"])

    def test_direct_process_hook_completion_is_terminal(self):
        run_id = str(uuid.uuid4())
        self.write(self.row(run_id, "started", component="process_hook"),
                   self.row(run_id, "completed", component="process_hook", status="ok", error_count=0))
        result = recent_hook_logs(self.home)
        self.assertEqual("available", result["status"])
        self.assertEqual(0, result["incomplete"])

    def test_supervisor_unfinished_after_worker_completion_remains_incomplete(self):
        run_id = str(uuid.uuid4())
        self.write(self.row(run_id, "started"),
                   self.row(run_id, "stage_started", component="worker", stage="semantic_capture"),
                   self.row(run_id, "completed", component="worker", status="ok", error_count=0))
        result = recent_hook_logs(self.home)
        self.assertEqual(1, result["incomplete"])
        self.assertEqual("semantic_capture", result["recent_incomplete"][0]["last_stages"]["worker"])

    def test_fallback_has_known_outcome_and_completion_code(self):
        run_id = str(uuid.uuid4())
        self.write(self.row(run_id, "started"),
                   self.row(run_id, "stage_started", component="worker", stage="persist"),
                   self.row(run_id, "completed", status="fallback", error_count=0, code="worker_failed"))
        result = recent_hook_logs(self.home)
        self.assertEqual(1, result["failures"])
        self.assertEqual(0, result["incomplete"])
        self.assertEqual("worker_failed", result["recent_failures"][0]["completion_codes"]["supervisor"])
        self.assertEqual("persist", result["recent_failures"][0]["last_stages"]["worker"])

    def test_failure_before_payload_reports_declared_event_and_elapsed_time(self):
        run_id = str(uuid.uuid4())
        self.write(self.row(run_id, "started", declared_hook_event="Stop"),
                   self.row(run_id, "stage_started", stage="worker_reap", declared_hook_event="Stop"))
        run = recent_hook_logs(self.home)["recent_incomplete"][0]
        self.assertEqual("Stop", run["declared_hook_event"])
        self.assertNotIn("hook_event", run)
        self.assertEqual(1.5, run["elapsed_ms"]["supervisor"])
        self.assertEqual("worker_reap", run["last_stages"]["supervisor"])

    def test_deep_json_and_huge_elapsed_fail_closed(self):
        run_id = str(uuid.uuid4())
        valid = self.row(run_id, "completed", status="ok", error_count=0)
        huge = json.dumps({**valid, "elapsed_ms": 10**400})
        deep = "[" * 1100 + "0" + "]" * 1100
        self.path.parent.mkdir(parents=True)
        self.path.write_text(huge + "\n" + deep + "\n" + json.dumps(valid) + "\n")
        result = recent_hook_logs(self.home)
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["sampled_runs"])
