"""Logging must explain failures without retaining content or blocking hooks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from codex_mem import hook_diagnostics as diagnostics


class HookDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "memory"
        patch = mock.patch.dict(os.environ, CODEX_MEM_DISABLED="0", CODEX_MEM_HOOK_LOG="1")
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(diagnostics.finish_trace)

    def rows(self) -> list[dict]:
        return [json.loads(line) for line in diagnostics.log_path(self.home).read_text().splitlines()]

    def test_safe_correlated_metadata_error_codes_and_stack(self) -> None:
        run_id = "01a0eea1-8e15-7b81-8b5b-18fb61f267bc"
        trace = diagnostics.begin_trace("worker", data_dir=self.home, run_id=run_id)
        diagnostics.trace_payload({"hook_event_name": "PostToolUse", "session_id": run_id,
            "turn_id": "PRIVATE_TURN", "cwd": "/PRIVATE_PROJECT", "tool_name": "PRIVATE_TOOL",
            "tool_use_id": "PRIVATE_CALL", "prompt": "PRIVATE_PROMPT",
            "tool_input": {"secret": "PRIVATE_INPUT"}, "tool_response": "PRIVATE_OUTPUT"})
        try:
            with diagnostics.trace_stage("store_open"):
                raise PermissionError(13, "PRIVATE_EXCEPTION_MESSAGE", "/PRIVATE_FILE")
        except PermissionError as exc:
            diagnostics.trace_error("storage_failed", exc)
        diagnostics.trace_event("operation", reason="safe_code", payload_bytes=123,
                                arbitrary_message="PRIVATE_MESSAGE")
        diagnostics.finish_trace()
        rows = self.rows()
        self.assertEqual(run_id, trace.run_id)
        self.assertTrue(all(row["run_id"] == run_id for row in rows))
        self.assertEqual("degraded", rows[-1]["status"])
        error = next(row["error"] for row in rows if row["event"] == "error")
        self.assertEqual("PermissionError", error["exception_type"])
        self.assertEqual("EACCES", error["errno_name"])
        self.assertEqual("store_open", error["failed_stage"])
        self.assertTrue(error["frames"])
        self.assertNotIn("PRIVATE_", json.dumps(rows))
        self.assertEqual(0o600, stat.S_IMODE(diagnostics.log_path(self.home).stat().st_mode))
        self.assertIsNone(diagnostics.get_trace())

    def test_sqlite_failure_preserves_machine_code_without_query(self) -> None:
        diagnostics.begin_trace("worker", data_dir=self.home)
        with sqlite3.connect(":memory:") as connection:
            try:
                connection.execute("SELECT PRIVATE_QUERY FROM PRIVATE_TABLE")
            except sqlite3.Error as exc:
                diagnostics.trace_error("storage_failed", exc)
        diagnostics.finish_trace()
        error = next(row["error"] for row in self.rows() if row["event"] == "error")
        self.assertEqual("SQLITE_ERROR", error["sqlite_errorname"])
        self.assertEqual(sqlite3.SQLITE_ERROR, error["sqlite_errorcode"])
        self.assertNotIn("PRIVATE_", json.dumps(self.rows()))

    def test_declared_event_is_available_before_payload_and_stays_distinct(self) -> None:
        with mock.patch.dict(os.environ, CODEX_MEM_HOOK_EVENT="Stop"):
            diagnostics.begin_trace("supervisor", data_dir=self.home)
            self.assertEqual("Stop", self.rows()[0]["declared_hook_event"])
            self.assertNotIn("hook_event", self.rows()[0])
            diagnostics.trace_payload({"hook_event_name": "PostToolUse"})
            diagnostics.finish_trace()
        self.assertEqual("Stop", self.rows()[-1]["declared_hook_event"])
        self.assertEqual("PostToolUse", self.rows()[-1]["hook_event"])

    def test_unknown_declared_event_does_not_enter_log(self) -> None:
        with mock.patch.dict(os.environ, CODEX_MEM_HOOK_EVENT="PRIVATE_EVENT_VALUE"):
            diagnostics.begin_trace("supervisor", data_dir=self.home)
            diagnostics.finish_trace()
        self.assertNotIn("PRIVATE_EVENT_VALUE", json.dumps(self.rows()))
        self.assertNotIn("declared_hook_event", self.rows()[0])

    def test_rotation_keeps_bounded_valid_private_files(self) -> None:
        with mock.patch.object(diagnostics, "MAX_LOG_BYTES", 1200):
            diagnostics.begin_trace("worker", data_dir=self.home)
            for index in range(50):
                diagnostics.trace_event("checkpoint", checkpoint=index)
            diagnostics.finish_trace()
        path = diagnostics.log_path(self.home)
        files = [path, *[Path(f"{path}.{i}") for i in range(1, diagnostics.LOG_BACKUPS + 1)]]
        for file in files:
            self.assertTrue(file.exists())
            self.assertLessEqual(file.stat().st_size, 1200)
            self.assertEqual(0o600, stat.S_IMODE(file.stat().st_mode))
            for line in file.read_text().splitlines():
                self.assertEqual(1, json.loads(line)["schema_version"])
        self.assertFalse(Path(f"{path}.{diagnostics.LOG_BACKUPS + 1}").exists())
        self.assertEqual("completed", self.rows()[-1]["event"])

    def test_disabled_logging_and_direct_helpers_do_not_create_directories(self) -> None:
        diagnostics.trace_event("checkpoint")
        with mock.patch.dict(os.environ, CODEX_MEM_HOOK_LOG="0"):
            diagnostics.begin_trace("worker", data_dir=self.home)
            diagnostics.trace_event("checkpoint")
            diagnostics.finish_trace()
        with mock.patch.dict(os.environ, CODEX_MEM_DISABLED="1"):
            diagnostics.begin_trace("worker", data_dir=self.home)
            diagnostics.finish_trace()
        self.assertFalse(self.home.exists())

    def test_unwritable_path_and_fifo_fail_open(self) -> None:
        self.home.parent.joinpath("not-directory").write_text("file")
        diagnostics.begin_trace("worker", data_dir=self.home.parent / "not-directory")
        diagnostics.trace_event("checkpoint")
        diagnostics.finish_trace()
        if not hasattr(os, "mkfifo"):
            return
        path = diagnostics.log_path(self.home)
        path.parent.mkdir(parents=True)
        os.mkfifo(path)
        started = time.monotonic()
        diagnostics.begin_trace("worker", data_dir=self.home)
        diagnostics.trace_event("checkpoint")
        diagnostics.finish_trace()
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(stat.S_ISFIFO(path.stat().st_mode))

    def test_parallel_process_appends_are_complete_json_records(self) -> None:
        env = dict(os.environ, CODEX_MEM_HOME=str(self.home))
        program = (
            "from codex_mem.hook_diagnostics import begin_trace, trace_event, finish_trace\n"
            "begin_trace('worker')\n"
            "for i in range(30): trace_event('checkpoint', checkpoint=i)\n"
            "finish_trace()\n"
        )
        processes = [subprocess.Popen([sys.executable, "-c", program], env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(0, process.returncode)
                self.assertEqual((b"", b""), (stdout, stderr))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()
        rows = self.rows()
        self.assertEqual(4 * 32, len(rows))
        self.assertEqual(4, len({row["run_id"] for row in rows}))


if __name__ == "__main__":
    unittest.main()
