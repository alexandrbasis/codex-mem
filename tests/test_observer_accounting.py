"""End-to-end accounting through real batch claims, status and native transports."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_mem.processor import MODEL, ProcessorFailure, process_pending
from codex_mem.store import Store, StoreError


ROOT = Path(__file__).resolve().parents[1]


def metrics(input_tokens=100, output_tokens=20, *, status="reported"):
    return {"duration_ms": 25, "usage": {
        "status": status, "source": "app_server_thread_total", "updates": 2,
        "tokens": {"input_tokens": input_tokens, "cached_input_tokens": 0,
                   "cache_write_input_tokens": 0, "output_tokens": output_tokens,
                   "reasoning_output_tokens": 0, "total_tokens": input_tokens + output_tokens},
    }}


def receipt(*, measured=True):
    value = {"output": {"disposition": "skipped", "notes": []}, "evidence": {
        "thread_start": {"thread_id": "thread-b", "model": MODEL,
                         "reasoning_effort": "medium", "model_provider": "openai"},
        "turn_started": {"thread_id": "thread-b", "turn_id": "turn-b"},
        "turn_completed": True, "no_tools": True, "rerouted": False,
    }}
    if measured:
        value["metrics"] = metrics(200, 40)
    return value


class ObserverAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.data = Path(self.temp.name) / "memory"
        with Store(self.data) as store:
            self.source = store.remember(self.project, "Fixture observation", "Fictional source text.",
                                         source="hook:PostToolUse", session_id="fixture-session")

    def summary(self):
        with Store(self.data) as store:
            return store.status(self.project)["observer_usage"]

    def test_failed_and_retried_model_attempts_are_kept_separately(self):
        def failed(request):
            raise ProcessorFailure("timeout", worker_thread_id="thread-a", worker_turn_id="turn-a",
                                   metrics=metrics(status="partial"))

        first = process_pending(self.project, self.data, runner=failed)
        second = process_pending(self.project, self.data, retry_failed=True, runner=lambda _: receipt())
        self.assertEqual("failed", first["status"])
        self.assertEqual("skipped", second["status"])
        self.assertEqual(first["job_id"], second["job_id"])
        summary = self.summary()
        self.assertEqual(2, summary["attempts"]["expected"])
        self.assertEqual(2, summary["attempts"]["recorded"])
        self.assertEqual(120, summary["totals"]["partial"]["total_tokens"])
        self.assertEqual(240, summary["totals"]["reported"]["total_tokens"])
        self.assertEqual(0, summary["attempts"]["without_receipt"])
        self.assertEqual("idle", process_pending(self.project, self.data, runner=lambda _: self.fail())["status"])
        self.assertEqual(summary, self.summary())
        with Store(self.data) as store:
            self.assertEqual("Fictional source text.", store.get(self.project, [self.source["id"]])[0]["body"])

    def test_legacy_runner_without_metrics_remains_unknown(self):
        result = process_pending(self.project, self.data, runner=lambda _: receipt(measured=False))
        self.assertEqual("skipped", result["status"])
        summary = self.summary()
        self.assertEqual(1, summary["attempts"]["unknown"])
        self.assertIsNone(summary["totals"]["reported"])

    def test_native_abort_preserves_checkpoint_before_retry(self):
        def native_runner(*, usage_checkpoint, **kwargs):
            def run(request):
                usage_checkpoint({"worker_thread_id": "thread-a", "worker_turn_id": "turn-a",
                                  "metrics": metrics(status="partial")})
                # A second independent Store connection sees the receipt while
                # the native call is still running, before any terminal write.
                during = self.summary()
                self.assertEqual(1, during["attempts"]["outcomes"]["running"])
                self.assertEqual(120, during["totals"]["partial"]["total_tokens"])
                raise RuntimeError("private abrupt worker failure")
            return run

        with mock.patch("codex_mem.processor.NativeProcessorRunner", side_effect=native_runner):
            failed = process_pending(self.project, self.data)
        self.assertEqual("runner_failure", failed["code"])
        self.assertNotIn("private", str(failed))
        self.assertEqual(120, self.summary()["totals"]["partial"]["total_tokens"])
        retried = process_pending(self.project, self.data, retry_failed=True, runner=lambda _: receipt())
        self.assertEqual("skipped", retried["status"])
        summary = self.summary()
        self.assertEqual(2, summary["attempts"]["recorded"])
        self.assertEqual(120, summary["totals"]["partial"]["total_tokens"])
        self.assertEqual(240, summary["totals"]["reported"]["total_tokens"])
        with Store(self.data) as store:
            row = store._connection.execute("SELECT worker_thread_id FROM observer_usage_attempts "
                                            "WHERE attempt_count=1").fetchone()
            self.assertEqual("thread-a", row[0])

    def test_rejected_content_still_has_reported_cost(self):
        value = receipt()
        value["output"] = {"disposition": "invalid", "notes": []}
        result = process_pending(self.project, self.data, runner=lambda _: value)
        self.assertEqual("invalid_response", result["code"])
        self.assertEqual(240, self.summary()["totals"]["reported"]["total_tokens"])
        self.assertEqual(1, self.summary()["attempts"]["outcomes"]["failed"])

    def test_malformed_metrics_do_not_leave_completed_attempt_running(self):
        value = receipt()
        value["metrics"]["usage"]["tokens"]["total_tokens"] = "untrusted"
        result = process_pending(self.project, self.data, runner=lambda _: value)
        self.assertEqual("skipped", result["status"])
        self.assertTrue(result["observer_usage_recorded"])
        summary = self.summary()
        self.assertEqual(1, summary["attempts"]["outcomes"]["skipped"])
        self.assertEqual(0, summary["attempts"]["outcomes"]["running"])
        self.assertEqual(1, summary["attempts"]["unknown"])
        self.assertIsNone(summary["totals"]["reported"])

    def test_timeout_after_lease_expiry_records_expired_outcome(self):
        def expired(request):
            with Store(self.data) as store:
                store._connection.execute(
                    "UPDATE observation_jobs SET lease_expires_at = '2000-01-01T00:00:00Z'")
                store._connection.commit()
            raise ProcessorFailure("timeout", metrics=metrics(status="partial"))

        result = process_pending(self.project, self.data, runner=expired)
        self.assertEqual("lease_expired", result["code"])
        summary = self.summary()
        self.assertEqual(1, summary["attempts"]["outcomes"]["lease_expired"])
        self.assertEqual(0, summary["attempts"]["outcomes"]["failed"])
        self.assertEqual(120, summary["totals"]["partial"]["total_tokens"])

    def test_reclaimed_attempt_keeps_late_usage_separate(self):
        def late(request):
            with Store(self.data) as store:
                store._connection.execute(
                    "UPDATE observation_jobs SET lease_expires_at = '2000-01-01T00:00:00Z'")
                store._connection.commit()
            retried = process_pending(self.project, self.data, runner=lambda _: receipt())
            self.assertEqual("skipped", retried["status"])
            during = self.summary()
            self.assertEqual(1, during["attempts"]["outcomes"]["lease_expired"])
            self.assertEqual(0, during["attempts"]["outcomes"]["running"])
            raise ProcessorFailure("timeout", metrics=metrics(status="partial"))

        result = process_pending(self.project, self.data, runner=late)
        # The Store rejects the superseded lease after attempt 2 completed.
        # Accounting still recognizes attempt 1 as expired and accepts its cost.
        self.assertEqual("storage_failure", result["code"])
        self.assertTrue(result["observer_usage_recorded"])
        summary = self.summary()
        self.assertEqual(2, summary["attempts"]["recorded"])
        self.assertEqual(1, summary["attempts"]["outcomes"]["lease_expired"])
        self.assertEqual(1, summary["attempts"]["outcomes"]["skipped"])
        self.assertEqual(120, summary["totals"]["partial"]["total_tokens"])
        self.assertEqual(240, summary["totals"]["reported"]["total_tokens"])

    def test_ledger_write_failure_does_not_relabel_completed_processing(self):
        with mock.patch("codex_mem.observer_usage_store.finish_attempt", side_effect=StoreError("write failed")):
            result = process_pending(self.project, self.data, runner=lambda _: receipt())
        self.assertEqual("skipped", result["status"])
        self.assertFalse(result["observer_usage_recorded"])
        self.assertEqual(1, self.summary()["attempts"]["unknown"])
        self.assertIsNone(self.summary()["totals"]["reported"])

    def test_retry_recovers_failed_job_when_its_terminal_usage_write_was_lost(self):
        value = receipt()
        value["output"] = {"disposition": "invalid", "notes": []}
        with mock.patch("codex_mem.observer_usage_store.finish_attempt", side_effect=StoreError("write failed")):
            first = process_pending(self.project, self.data, runner=lambda _: value)
        self.assertEqual("invalid_response", first["code"])
        second = process_pending(self.project, self.data, retry_failed=True, runner=lambda _: receipt())
        self.assertEqual("skipped", second["status"])
        summary = self.summary()
        self.assertEqual(1, summary["attempts"]["outcomes"]["failed"])
        self.assertEqual(0, summary["attempts"]["outcomes"]["lease_expired"])
        self.assertEqual(1, summary["attempts"]["unknown"])
        self.assertEqual(240, summary["totals"]["reported"]["total_tokens"])

    def test_cli_and_mcp_expose_separate_project_accounting(self):
        process_pending(self.project, self.data, runner=lambda _: receipt())
        cli = subprocess.run([sys.executable, "scripts/codex-mem.py", "--data-dir", str(self.data),
                              "usage", "status", "--project", str(self.project)],
                             cwd=ROOT, capture_output=True, text=True, check=True, timeout=10)
        result = json.loads(cli.stdout)
        self.assertEqual([], result["records"])
        self.assertEqual(240, result["observer_usage"]["totals"]["reported"]["total_tokens"])
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "observer-fixture", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_status", "arguments": {"project": str(self.project)}}},
        ]
        mcp = subprocess.run([sys.executable, "-m", "codex_mem.mcp", "--data-dir", str(self.data)],
                             cwd=ROOT, input="".join(json.dumps(m) + "\n" for m in messages),
                             capture_output=True, text=True, check=True, timeout=10)
        response = next(json.loads(line) for line in mcp.stdout.splitlines() if json.loads(line).get("id") == 2)
        self.assertNotIn("error", response)
        structured = response["result"].get("structuredContent")
        if structured is None:
            structured = json.loads(response["result"]["content"][0]["text"])
        if set(structured) == {"result"}:
            structured = structured["result"]
        self.assertEqual(240, structured["observer_usage"]["totals"]["reported"]["total_tokens"])
        with Store(self.data) as store:
            foreign = store.status(Path(self.temp.name) / "other")["observer_usage"]
        self.assertEqual(0, foreign["attempts"]["expected"])
        self.assertIsNone(foreign["totals"]["reported"])


if __name__ == "__main__":
    unittest.main()
