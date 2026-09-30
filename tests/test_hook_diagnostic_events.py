"""Content-free hook diagnostics at the fail-open boundaries."""

from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem.hook_diagnostics import begin_trace, finish_trace, get_trace
from codex_mem.hooks import handle_hook, handle_process_hook, main


class _Store:
    def __init__(self, data_dir: Path, *, context_error: bool = False,
                 remember_error: bool = False) -> None:
        self.data_dir = data_dir
        self.context_error = context_error
        self.remember_error = remember_error

    def context(self, *_args: object, **_kwargs: object) -> str:
        if self.context_error:
            raise RuntimeError("PRIVATE_CONTEXT_EXCEPTION_TEXT")
        return "safe context"

    def remember(self, *_args: object, **_kwargs: object) -> None:
        if self.remember_error:
            raise RuntimeError("PRIVATE_REMEMBER_EXCEPTION_TEXT")


class HookDiagnosticEventsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "memory"
        self.project = self.root / "project"
        configure(self.data_dir, capture_scope="selected",
                  included_projects=[self.project])
        self.env = mock.patch.dict("os.environ", {"CODEX_MEM_HOOK_LOG": "1",
                                                 "CODEX_MEM_DISABLED": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self._finish_active_trace)

    @staticmethod
    def _finish_active_trace() -> None:
        if get_trace() is not None:
            finish_trace()

    def _payload(self, event: str, **extra: object) -> dict[str, object]:
        return {"hook_event_name": event, "cwd": str(self.project),
                "session_id": "session-a", "turn_id": "turn-a", **extra}

    def _records(self) -> list[dict[str, object]]:
        path = self.data_dir / "logs" / "hooks.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def _trace_call(self, payload: dict[str, object], store: object | None) -> list[dict[str, object]]:
        begin_trace("hook", data_dir=self.data_dir)
        self.assertTrue(handle_hook(payload, store)["continue"])
        finish_trace()
        return self._records()

    def test_context_exception_records_safe_failure_and_continues(self) -> None:
        rows = self._trace_call(self._payload("SessionStart", source="startup"),
                                _Store(self.data_dir, context_error=True))
        self.assertIn("context_read_failed", [row.get("code") for row in rows])
        self.assertTrue(any(row.get("event") == "stage_started" and
                            row.get("stage") == "context_read" for row in rows))
        self.assertEqual("degraded", rows[-1]["status"])
        self.assertNotIn("PRIVATE_CONTEXT_EXCEPTION_TEXT", json.dumps(rows))

    def test_remember_and_queue_failures_are_distinct(self) -> None:
        payload = self._payload("Stop", last_assistant_message="SAFE_FINAL")
        rows = self._trace_call(payload, _Store(self.data_dir, remember_error=True))
        self.assertIn("capture_remember_failed", [row.get("code") for row in rows])

        (self.data_dir / "logs" / "hooks.jsonl").unlink()
        with mock.patch("codex_mem.integration.after_write",
                        side_effect=RuntimeError("PRIVATE_QUEUE_EXCEPTION_TEXT")):
            rows = self._trace_call(payload, _Store(self.data_dir))
        self.assertIn("queue_wake_failed", [row.get("code") for row in rows])
        self.assertNotIn("PRIVATE_QUEUE_EXCEPTION_TEXT", json.dumps(rows))
        self.assertNotIn("SAFE_FINAL", json.dumps(rows))

    def test_scope_and_tool_skip_reasons_do_not_open_store(self) -> None:
        outside = self.root / "outside"
        payload = self._payload("Stop", cwd=str(outside),
                                last_assistant_message="SECRET_OUTSIDE")
        rows = self._trace_call(payload, _Store(self.data_dir))
        self.assertIn("project_not_captured", [row.get("reason") for row in rows])
        self.assertNotIn("SECRET_OUTSIDE", json.dumps(rows))

        (self.data_dir / "logs" / "hooks.jsonl").unlink()
        configure(self.data_dir, capture_tools=False)
        rows = self._trace_call(self._payload("PostToolUse", tool_name="Bash",
            tool_input={"command": "echo PRIVATE_COMMAND"},
            tool_response={"output": "PRIVATE_OUTPUT"}), _Store(self.data_dir))
        self.assertIn("tool_capture_disabled", [row.get("reason") for row in rows])
        self.assertNotIn("PRIVATE_COMMAND", json.dumps(rows))
        self.assertNotIn("PRIVATE_OUTPUT", json.dumps(rows))

    def test_store_open_failure_and_malformed_main_are_logged_after_stdout(self) -> None:
        begin_trace("hook", data_dir=self.data_dir)
        with mock.patch("codex_mem.hooks.Store", side_effect=RuntimeError("PRIVATE_STORE_EXCEPTION_TEXT")):
            self.assertEqual({"continue": True}, handle_hook(
                self._payload("SessionStart", source="startup")))
        finish_trace()
        rows = self._records()
        self.assertIn("hook_unavailable", [row.get("code") for row in rows])
        self.assertNotIn("PRIVATE_STORE_EXCEPTION_TEXT", json.dumps(rows))

        (self.data_dir / "logs" / "hooks.jsonl").unlink()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch("sys.stdin", io.StringIO('{"secret":"PRIVATE_INPUT"')),
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
        ):
            self.assertEqual(0, main(data_dir=self.data_dir))
        self.assertEqual({"continue": True}, json.loads(stdout.getvalue()))
        rows = self._records()
        self.assertIn("stdin_json_parse_failed", [row.get("code") for row in rows])
        self.assertEqual("invalid_input", rows[-1]["status"])
        self.assertTrue(any(row.get("event") == "stdout_written" for row in rows))
        self.assertNotIn("PRIVATE_INPUT", json.dumps(rows))

    def test_async_result_reports_only_recognized_return_status(self) -> None:
        payload = self._payload("Stop")
        for returned, expected in (
            ({"status": "queued"}, "queued"),
            ({"status": "processed"}, "processed"),
            ({"status": "invented"}, "unknown"),
            (None, "unknown"),
            ({"status": "failed", "code": "PRIVATE_FAILURE_CODE"}, "failed"),
        ):
            with self.subTest(returned=returned):
                path = self.data_dir / "logs" / "hooks.jsonl"
                if path.exists():
                    path.unlink()
                begin_trace("process_hook", data_dir=self.data_dir)
                with mock.patch("codex_mem.hooks._run_pending_processor", return_value=returned):
                    self.assertEqual({"continue": True}, handle_process_hook(
                        payload, _Store(self.data_dir), data_dir=self.data_dir))
                finish_trace()
                rows = self._records()
                statuses = [row.get("status") for row in rows
                            if row.get("event") == "processor_result"]
                self.assertEqual([expected], statuses)
                self.assertNotIn("PRIVATE_FAILURE_CODE", json.dumps(rows))
                if expected == "failed":
                    self.assertIn("processor_reported_failure",
                                  [row.get("code") for row in rows])
                    self.assertEqual("degraded", rows[-1].get("status"))


if __name__ == "__main__":
    unittest.main()
