"""Dependency failures retain safe diagnostics needed for bounded recovery."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem.jev_filter import JevFilterError
from codex_mem.processor import ProcessorFailure, _TurnMonitor, process_pending
from codex_mem.store import Store


class RunnerFailureReasonTests(unittest.TestCase):
    def test_jev_failures_survive_receipt_and_storage_without_starting_generator(self):
        for reason in ("jev_filter_invalid_response", "jev_filter_transport", "jev_filter_credentials"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                project, home = Path(root) / "project", Path(root) / "memory"
                project.mkdir()
                configure(home, jev_filter_enabled=True)
                with Store(home) as store:
                    raw = store.remember(project, "Event", "PRIVATE_SOURCE", source="hook:PostToolUse")
                runner = mock.Mock(side_effect=AssertionError("must not generate"))
                evaluator = mock.Mock(side_effect=JevFilterError(reason))
                result = process_pending(project, home, runner=runner, jev_evaluator=evaluator)
                self.assertEqual("runner_failure", result["code"])
                self.assertEqual(reason, result.get("reason_code"))
                runner.assert_not_called()
                with Store(home) as store:
                    job = store.observation_job_status(project, result["job_id"])
                    self.assertEqual(reason, job["failure_receipts"][-1]["reason_code"])
                    self.assertEqual("PRIVATE_SOURCE", store.get(project, [raw["id"]])[0]["body"])
                self.assertNotIn("PRIVATE_SOURCE", json.dumps([result, job]))

    def test_native_turn_errors_use_protocol_codes_and_never_remote_text(self):
        cases = [
            ("rateLimitExceeded", "native_rate_limit"),
            ("usageLimitExceeded", "native_usage_limit"),
            ("sessionBudgetExceeded", "native_usage_limit"),
            ("unauthorized", "native_auth"),
            ("contextWindowExceeded", "native_context_limit"),
            ("badRequest", "native_bad_request"),
            ("cyberPolicy", "native_policy"),
            ("serverOverloaded", "native_server_error"),
            ("internalServerError", "native_server_error"),
            ({"httpConnectionFailed": {"httpStatusCode": 401}}, "native_auth"),
            ({"responseStreamConnectionFailed": {"httpStatusCode": 429}}, "native_rate_limit"),
            ({"responseStreamDisconnected": {"httpStatusCode": 503}}, "native_server_error"),
            ({"httpConnectionFailed": {"httpStatusCode": None}}, "native_connection_error"),
            ({"httpConnectionFailed": {"httpStatusCode": 400}}, "native_bad_request"),
            ({"httpConnectionFailed": {"httpStatusCode": "PRIVATE_SECRET"}}, "native_turn_failed"),
            ("PRIVATE_SECRET", "native_turn_failed"),
            ({"PRIVATE_SECRET": "PRIVATE_SECRET"}, "native_turn_failed"),
        ]
        for info, reason in cases:
            with self.subTest(info=info):
                monitor = _TurnMonitor("worker")
                monitor.set_turn("turn")
                with self.assertRaises(ProcessorFailure) as caught:
                    monitor.observe({"method": "turn/completed", "params": {
                        "threadId": "worker", "turn": {"id": "turn", "status": "failed", "items": [],
                        "error": {"codexErrorInfo": info, "message": "PRIVATE_SECRET"}}}})
                self.assertEqual("runner_failure", caught.exception.code)
                self.assertEqual(reason, caught.exception.reason_code)
                self.assertNotIn("PRIVATE_SECRET", str(caught.exception))

    def test_interrupted_turn_is_not_treated_as_transient_error(self):
        monitor = _TurnMonitor("worker")
        monitor.set_turn("turn")
        with self.assertRaises(ProcessorFailure) as caught:
            monitor.observe({"method": "turn/completed", "params": {
                "threadId": "worker", "turn": {"id": "turn", "status": "interrupted", "items": []}}})
        self.assertEqual("native_turn_cancelled", caught.exception.reason_code)

    def test_unknown_and_cross_category_reason_strings_are_never_persisted(self):
        for code, reason in (("runner_failure", "PRIVATE_SECRET"),
                             ("invalid_response", "native_auth"),
                             ("runner_failure", "invalid_json")):
            self.assertIsNone(ProcessorFailure(code, reason_code=reason).reason_code)


if __name__ == "__main__":
    unittest.main()
