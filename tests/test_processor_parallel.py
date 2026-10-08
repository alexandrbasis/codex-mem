"""Concurrent processor calls use separate leases, runners and usage receipts."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from codex_mem.processor import MODEL, REASONING_EFFORT, process_pending
from codex_mem.store import Store


class ParallelProcessorTests(unittest.TestCase):
    def test_two_sessions_reach_runner_together_and_keep_separate_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "memory"
            project = Path(temporary) / "project"
            project.mkdir()
            with Store(home) as store:
                for session in ("alpha", "beta"):
                    store.remember(project, "Fictional request", "Hello from " + session,
                                   source="hook:UserPromptSubmit", session_id=session)
            rendezvous = threading.Barrier(2)

            def runner(request):
                rendezvous.wait(timeout=5)
                identifier = request["job_id"]
                return {"output": {"disposition": "skipped", "notes": [], "session_summary": None},
                        "evidence": {
                            "thread_start": {"thread_id": identifier, "model": MODEL,
                                             "reasoning_effort": REASONING_EFFORT,
                                             "model_provider": "openai"},
                            "turn_started": {"thread_id": identifier, "turn_id": identifier},
                            "turn_completed": True, "no_tools": True, "rerouted": False}}

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: process_pending(
                    project, home, runner=runner, parallel_sessions=True), range(2)))
            self.assertEqual(["skipped", "skipped"], [row["status"] for row in results])
            self.assertEqual(2, len({row["job_id"] for row in results}))
            with Store(home) as store:
                jobs = store._connection.execute(
                    "SELECT session_id,status,worker_thread_id FROM observation_jobs").fetchall()
                self.assertEqual({"alpha", "beta"}, {row[0] for row in jobs})
                self.assertEqual({"skipped"}, {row[1] for row in jobs})
                self.assertEqual(2, len({row[2] for row in jobs}))
                attempts = store._connection.execute(
                    "SELECT job_id,outcome FROM observer_usage_attempts").fetchall()
                self.assertEqual({row["job_id"] for row in results}, {row[0] for row in attempts})
                self.assertEqual({"skipped"}, {row[1] for row in attempts})


if __name__ == "__main__":
    unittest.main()
