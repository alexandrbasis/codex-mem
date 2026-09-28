"""Quality content failures isolate a batch; provider failure halts its project."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_mem.config import configure
from codex_mem.jev_quality import MAX_QUALITY_PAYLOAD_BYTES
from codex_mem.processor import process_pending
from codex_mem.service import SERVICE_STATE_FILENAME, enqueue, run_service, service_status
from codex_mem.store import Store
from tests import test_processor as processor_fixtures
from tests.test_jev_quality import note, response
from tests.test_service import Clock


class JevQualityServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.home = self.root / "memory"
        self.workspace = str(self.project.resolve())
        configure(self.home, capture_scope="selected", included_projects=[self.project],
                  semantic_enabled=False, jev_quality_enabled=True)
        self.clock = Clock()
        self.generator_calls = []
        self.evaluator_calls = []
        self.processor_retries = []
        self.receipts = []

    def seed(self, reason):
        body = "PRIVATE_SOURCE The user requested a production deployment."
        if reason == "input_limit":
            body += "x" * MAX_QUALITY_PAYLOAD_BYTES
        with Store(self.home) as store:
            bad = store.remember(self.project, "Rejected batch", body,
                                 source="hook:UserPromptSubmit", session_id="earlier-session")
            good = store.remember(self.project, "Independent new batch", "The synthetic check passed.",
                                  source="hook:PostToolUse", session_id="later-session")
        self.assertEqual("queued", enqueue(self.project, self.home, clock=self.clock)["status"])
        return bad, good

    def processor(self, reason):
        def runner(request):
            source = request["sources"][0]
            self.generator_calls.append(source["title"])
            candidate = note(source["id"], title="Deployment completed", body="Production deployment completed.")
            if source["title"] == "Independent new batch":
                candidate = note(source["id"], title="Synthetic check passed", body="The synthetic check passed.")
            return processor_fixtures.ProcessorTests.receipt(
                {"disposition": "processed", "notes": [candidate], "session_summary": None})

        def evaluator(payload):
            title = payload["state"]["items"][0]["cited_sources"][0]["title"]
            self.evaluator_calls.append(title)
            if title == "Independent new batch":
                return response(payload)
            if reason == "unavailable":
                raise RuntimeError("PRIVATE_PROVIDER_UNAVAILABLE")
            return response(payload, .01, .99) if reason == "rejected" else response(payload, .5, .1)

        def process(project, **kwargs):
            self.processor_retries.append(bool(kwargs["retry_failed"]))
            receipt = process_pending(project, runner=runner, jev_quality_evaluator=evaluator, **kwargs)
            self.receipts.append(receipt)
            return receipt
        return process

    def run_queue(self, processor):
        return run_service(self.home, processor=processor, clock=self.clock,
                           sleeper=self.clock.sleep, poll_interval=1, max_cycles=8)

    def assert_batch_isolated(self, reason, first_calls):
        bad, good = self.seed(reason)
        processor = self.processor(reason)
        result = self.run_queue(processor)
        self.assertEqual("cycle_limit", result["status"], result)
        self.assertEqual(["failed", "processed", "idle"], [row["status"] for row in self.receipts])
        self.assertEqual("jev_quality_" + reason, self.receipts[0]["reason_code"])
        self.assertEqual(first_calls, self.receipts[0]["jev_quality"]["counts"]["requests"])
        self.assertEqual(["Rejected batch", "Independent new batch"], self.generator_calls)
        self.assertEqual([False, False, False], self.processor_retries)
        with Store(self.home) as store:
            failed = store.observation_job_status(self.project, self.receipts[0]["job_id"])
            self.assertEqual("failed", failed["status"])
            self.assertEqual(1, failed["attempt_count"])
            self.assertEqual([], failed["output_ids"])
            self.assertEqual("jev_quality_" + reason, failed["failure_receipts"][-1]["reason_code"])
            self.assertIsNone(store.get(self.project, [bad["id"]])[0]["superseded_by"])
            self.assertIsNotNone(store.get(self.project, [good["id"]])[0]["superseded_by"])
        status = service_status(self.home, clock=self.clock)
        self.assertEqual(0, status["blocked_projects"])
        self.assertEqual(1, status["quarantined_batches"])
        self.assertEqual("jev_quality_" + reason, status["quarantines"][self.workspace]["reason_code"])
        self.assertNotIn("PRIVATE_", (self.home / SERVICE_STATE_FILENAME).read_text())
        # A normal later wake-up may look for new work, but never rearms this batch.
        self.assertEqual("queued", enqueue(self.project, self.home, clock=self.clock)["status"])
        self.run_queue(processor)
        self.assertEqual(["Rejected batch", "Independent new batch"], self.generator_calls)
        self.assertEqual([False, False, False, False], self.processor_retries)
        with Store(self.home) as store:
            self.assertEqual(1, store.observation_job_status(self.project, failed["job_id"])["attempt_count"])

    def test_rejected_quality_batch_does_not_block_independent_new_work(self):
        self.assert_batch_isolated("rejected", 1)

    def test_uncertain_quality_batch_does_not_block_independent_new_work(self):
        self.assert_batch_isolated("uncertain", 2)

    def test_oversized_quality_batch_does_not_block_independent_new_work(self):
        self.assert_batch_isolated("input_limit", 0)

    def test_unavailable_quality_service_halts_project_without_more_generation(self):
        bad, good = self.seed("unavailable")
        processor = self.processor("unavailable")
        result = self.run_queue(processor)
        self.assertEqual({"status": "halted", "jobs": 1, "code": "invalid_response"}, result)
        self.assertEqual("jev_quality_unavailable", self.receipts[0]["reason_code"])
        self.assertEqual(["Rejected batch"], self.generator_calls)
        self.assertEqual(["Rejected batch"], self.evaluator_calls)
        self.assertEqual([False], self.processor_retries)
        status = service_status(self.home, clock=self.clock)
        self.assertEqual(1, status["blocked_projects"])
        with Store(self.home) as store:
            job = store.observation_job_status(self.project, self.receipts[0]["job_id"])
            self.assertEqual("failed", job["status"])
            self.assertEqual(1, job["attempt_count"])
            self.assertEqual([], job["output_ids"])
            self.assertTrue(all(row["superseded_by"] is None for row in store.get(self.project, [bad["id"], good["id"]])))
        self.assertEqual("blocked", enqueue(self.project, self.home, clock=self.clock)["status"])
        self.run_queue(processor)
        self.assertEqual(["Rejected batch"], self.generator_calls)
        self.assertEqual([False], self.processor_retries)
        self.assertNotIn("PRIVATE_", json.dumps(service_status(self.home, clock=self.clock)))


if __name__ == "__main__":
    unittest.main()
