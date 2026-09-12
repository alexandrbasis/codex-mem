"""Queue recovery regressions with real leases and deterministic model calls."""

from pathlib import Path
import tempfile
import unittest

from codex_mem.config import configure
from codex_mem.processor import (
    MODEL,
    PROCESSOR_ID,
    REASONING_EFFORT,
    ProcessorFailure,
    process_pending,
)
from codex_mem.service import enqueue, run_service
from codex_mem.store import Store


class Clock:
    def __init__(self):
        self.value = 1_000.0

    def __call__(self):
        return self.value

    def sleep(self, delay):
        self.value += delay


class QueueRecoveryTests(unittest.TestCase):
    def test_exact_timeout_retry_never_authorizes_other_failed_jobs(self):
        for selector in ("missing", "foreign", "rejected"):
            with self.subTest(selector=selector), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                project = root / "project"
                foreign_project = root / "foreign"
                project.mkdir()
                foreign_project.mkdir()
                with Store(root / "memory") as store:
                    failed_jobs = {}
                    for name, owner, code in (
                        ("rejected", project, "invalid_response"),
                        ("timeout", project, "timeout"),
                        ("foreign", foreign_project, "timeout"),
                    ):
                        store.remember(
                            owner, name, name, source="hook:PostToolUse", session_id=name,
                        )
                        job = store.claim_observation_batch(
                            owner, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                        )
                        store.fail_observation_batch(owner, job["job_id"], job["lease_token"], code)
                        failed_jobs[name] = job["job_id"]
                    fresh = store.remember(
                        project, "fresh", "fresh evidence",
                        source="hook:PostToolUse", session_id="fresh-session",
                    )
                    requested_id = failed_jobs.get(selector, "f" * 32)
                    claimed = store.claim_observation_batch(
                        project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                        retry_job_id=requested_id,
                    )
                    self.assertEqual([fresh["id"]], [item["id"] for item in claimed["sources"]])
                    for failed_id in failed_jobs.values():
                        row = store._connection.execute(
                            "SELECT status,attempt_count FROM observation_jobs WHERE id=?",
                            (failed_id,),
                        ).fetchone()
                        self.assertEqual(("failed", 1), tuple(row))
                    with self.assertRaises(ValueError):
                        store.claim_observation_batch(
                            project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                            retry_failed=True, retry_job_id=requested_id,
                        )

    def test_automatic_timeout_retry_never_reopens_an_older_content_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            data_dir = root / "memory"
            clock = Clock()
            configure(data_dir, capture_scope="selected", included_projects=[project])
            with Store(data_dir) as store:
                store.remember(
                    project, "Rejected evidence", "older rejected content",
                    source="hook:PostToolUse", session_id="older-session",
                )
                rejected = store.claim_observation_batch(
                    project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                )
                store.fail_observation_batch(
                    project, rejected["job_id"], rejected["lease_token"], "invalid_response",
                )
                store.remember(
                    project, "New evidence", "new retryable content",
                    source="hook:PostToolUse", session_id="new-session",
                )

            seen_sources = []
            receipts = []

            def runner(request):
                seen_sources.append([item["body"] for item in request["sources"]])
                if len(seen_sources) == 1:
                    raise ProcessorFailure("timeout")
                return {
                    "output": {"notes": [], "disposition": "skipped"},
                    "evidence": {
                        "thread_start": {
                            "thread_id": "worker-thread", "model": MODEL,
                            "reasoning_effort": REASONING_EFFORT,
                            "model_provider": "openai",
                        },
                        "turn_started": {"thread_id": "worker-thread", "turn_id": "worker-turn"},
                        "turn_completed": True, "no_tools": True, "rerouted": False,
                    },
                }

            def processor(selected_project, **kwargs):
                receipt = process_pending(selected_project, runner=runner, **kwargs)
                receipts.append(receipt)
                return receipt

            enqueue(project, data_dir, clock=clock)
            run_service(
                data_dir, processor=processor, clock=clock, sleeper=clock.sleep,
                max_timeout_retries=1, retry_backoff=1, poll_interval=1, max_cycles=4,
            )

            self.assertEqual(
                [["new retryable content"], ["new retryable content"]], seen_sources,
            )
            self.assertEqual("timeout", receipts[0]["code"])
            self.assertEqual("skipped", receipts[1]["status"])
            self.assertEqual(receipts[0]["job_id"], receipts[1]["job_id"])
            with Store(data_dir) as store:
                jobs = {
                    row["id"]: dict(row)
                    for row in store._connection.execute(
                        "SELECT id,status,error_code,attempt_count FROM observation_jobs"
                    )
                }
            self.assertEqual("failed", jobs[rejected["job_id"]]["status"])
            self.assertEqual("invalid_response", jobs[rejected["job_id"]]["error_code"])
            self.assertEqual(1, jobs[rejected["job_id"]]["attempt_count"])
            self.assertEqual("skipped", jobs[receipts[0]["job_id"]]["status"])
            self.assertEqual(2, jobs[receipts[0]["job_id"]]["attempt_count"])


if __name__ == "__main__":
    unittest.main()
