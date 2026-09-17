from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem.service import (
    SERVICE_STATE_FILENAME,
    enqueue,
    run_service,
    service_status,
    stop_service,
)


class ServiceFailureDetailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.workspace = str(self.project.resolve())
        self.data_dir = self.root / "memory"
        self.now = 1_000.0
        configure(self.data_dir, capture_scope="selected", included_projects=[self.project])

    def clock(self) -> float:
        return self.now

    def state(self) -> dict:
        return json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())

    def test_index_backend_failure_survives_stop_and_state_reload(self) -> None:
        enqueue(self.project, self.data_dir, clock=self.clock)

        def indexer(*_args: object) -> dict:
            self.assertEqual("stopping", stop_service(self.data_dir, clock=self.clock)["status"])
            return {
                "status": "failed", "code": "embedding_failed", "pending": 0,
                "detail": "private observation content", "stderr": "secret-access-token",
            }

        result = run_service(
            self.data_dir, processor=lambda *a, **k: {"status": "idle"},
            indexer=indexer, clock=self.clock, max_cycles=1,
        )
        self.assertEqual({"status": "halted", "jobs": 1, "code": "index_failure"}, result)
        record = self.state()["projects"][self.workspace]
        self.assertEqual(self.now, record.get("last_failure_at"))
        detail = {"stage": "index", "status": "failed", "code": "embedding_failed"}
        self.assertEqual(detail, record.get("last_failure_detail"))

        # A later service lifecycle reloads and rewrites the state. It must
        # retain diagnostics without retrying a blocked project.
        def unexpected_call(*_args: object, **_kwargs: object) -> None:
            self.fail("a blocked project must require explicit recovery")

        run_service(
            self.data_dir, processor=unexpected_call, indexer=unexpected_call,
            clock=self.clock, sleeper=lambda _delay: None, max_cycles=1,
        )
        status = service_status(self.data_dir, clock=self.clock)
        self.assertEqual("stopped", status["status"])
        self.assertEqual(1, status["blocked_projects"])
        self.assertEqual({
            "code": "index_failure", "last_failure_at": self.now, "detail": detail,
        }, status["failures"][self.workspace])
        persisted = (self.data_dir / SERVICE_STATE_FILENAME).read_text()
        self.assertNotIn("private observation content", persisted)
        self.assertNotIn("secret-access-token", persisted)

    def test_index_receipt_only_preserves_allowlisted_codes_and_statuses(self) -> None:
        cases = (
            ({"status": "error", "code": "storage_failure"}, "error", "storage_failure"),
            ({"status": "failed", "code": "secret-access-token"}, "failed", None),
            ({"status": "failed", "code": {"token": "secret-access-token"}}, "failed", None),
            ({"status": "secret-access-token", "code": "embedding_failed"}, None, None),
        )
        for receipt, expected_status, expected_code in cases:
            with self.subTest(receipt=receipt):
                enqueue(self.project, self.data_dir, retry_failed=True, clock=self.clock)
                result = run_service(
                    self.data_dir, processor=lambda *a, **k: {"status": "idle"},
                    indexer=lambda *a, **k: {**receipt, "detail": "secret-access-token"},
                    clock=self.clock, max_cycles=1,
                )
                self.assertEqual("index_failure", result["code"])
                record = self.state()["projects"][self.workspace]
                self.assertEqual({
                    "stage": "index", "status": expected_status, "code": expected_code,
                }, record["last_failure_detail"])
                self.assertNotIn("secret-access-token", (self.data_dir / SERVICE_STATE_FILENAME).read_text())

    def test_index_exception_does_not_persist_its_text_or_attributes(self) -> None:
        enqueue(self.project, self.data_dir, clock=self.clock)

        def indexer(*_args: object) -> None:
            error = RuntimeError("secret-access-token")
            error.code = "embedding_failed"
            raise error

        run_service(
            self.data_dir, processor=lambda *a, **k: {"status": "idle"},
            indexer=indexer, clock=self.clock, max_cycles=1,
        )
        record = self.state()["projects"][self.workspace]
        self.assertEqual({"stage": "index", "status": None, "code": None}, record["last_failure_detail"])
        self.assertNotIn("secret-access-token", (self.data_dir / SERVICE_STATE_FILENAME).read_text())

    def test_legacy_failure_keeps_unknown_detail_without_inventing_a_timestamp(self) -> None:
        enqueue(self.project, self.data_dir, clock=self.clock)
        state = self.state()
        record = state["projects"][self.workspace]
        record["blocked"] = True
        record["last_code"] = "index_failure"
        record.pop("last_failure_at", None)
        record.pop("last_failure_detail", None)
        (self.data_dir / SERVICE_STATE_FILENAME).write_text(json.dumps(state))

        status = service_status(self.data_dir, clock=self.clock)
        self.assertEqual("stopped", status["status"])
        self.assertEqual({"code": "index_failure", "last_failure_at": None, "detail": None},
                         status["failures"][self.workspace])

    def test_runner_exception_replaces_prior_index_detail_without_guessing_a_cause(self) -> None:
        enqueue(self.project, self.data_dir, clock=self.clock)
        run_service(
            self.data_dir, processor=lambda *a, **k: {"status": "idle"},
            indexer=lambda *a, **k: {"status": "failed", "code": "embedding_failed"},
            clock=self.clock, max_cycles=1,
        )
        self.now = 2_000.0
        enqueue(self.project, self.data_dir, retry_failed=True, clock=self.clock)

        def processor(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("secret-access-token")

        run_service(self.data_dir, processor=processor, clock=self.clock, max_cycles=1)
        status = service_status(self.data_dir, clock=self.clock)
        expected = {"code": "runner_failure", "last_failure_at": self.now, "detail": None}
        self.assertEqual(expected, status["failures"][self.workspace])
        self.assertNotIn("secret-access-token", (self.data_dir / SERVICE_STATE_FILENAME).read_text())
        with mock.patch("codex_mem.service._pid_lock_held", return_value=None):
            status = service_status(self.data_dir, clock=self.clock)
        self.assertEqual("unknown", status["status"])
        self.assertEqual(expected, status["failures"][self.workspace])

    def test_recovery_removes_active_failure_from_status(self) -> None:
        enqueue(self.project, self.data_dir, clock=self.clock)
        run_service(
            self.data_dir, processor=lambda *a, **k: {"status": "idle"},
            indexer=lambda *a, **k: {"status": "failed", "code": "embedding_failed"},
            clock=self.clock, max_cycles=1,
        )
        enqueue(self.project, self.data_dir, retry_failed=True, clock=self.clock)
        run_service(
            self.data_dir, processor=lambda *a, **k: {"status": "idle"},
            indexer=lambda *a, **k: {"status": "indexed", "pending": 0},
            clock=self.clock, max_cycles=1,
        )
        status = service_status(self.data_dir, clock=self.clock)
        self.assertEqual(0, status["blocked_projects"])
        self.assertEqual({}, status["failures"])


if __name__ == "__main__":
    unittest.main()
