"""Session claims remain ordered and exclusive across Store connections."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from codex_mem.processor import MODEL, PROCESSOR_ID, REASONING_EFFORT
from codex_mem.store import Store


class StoreParallelSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.data = self.root / "memory"
        self.store = Store(self.data)
        self.addCleanup(self.store.close)

    def raw(self, session, *, source="hook:PostToolUse", body="Complete raw evidence"):
        return self.store.remember(
            self.project, "Raw source", body, source=source, session_id=session,
        )

    def claim(self, *, store=None, processor=PROCESSOR_ID, **kwargs):
        return (store or self.store).claim_observation_batch(
            self.project, processor, MODEL, REASONING_EFFORT,
            parallel_sessions=kwargs.pop("parallel_sessions", True), **kwargs,
        )

    def fail(self, job, code="timeout"):
        self.store.fail_observation_batch(
            self.project, job["job_id"], job["lease_token"], code,
        )

    def expire(self, job):
        self.store._connection.execute(
            "UPDATE observation_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?",
            (job["job_id"],),
        )

    def race(self, options):
        barrier = threading.Barrier(len(options))

        def run(kwargs):
            with Store(self.data) as worker:
                barrier.wait(timeout=10)
                return self.claim(store=worker, **kwargs)

        with ThreadPoolExecutor(max_workers=len(options)) as pool:
            return list(pool.map(run, options))

    def test_different_sessions_claim_concurrently_across_connections(self):
        raws = [self.raw(session) for session in ("a", "b")]
        jobs = self.race([{}, {}])
        self.assertTrue(all(jobs))
        self.assertEqual({raw["id"] for raw in raws},
                         {job["sources"][0]["id"] for job in jobs})
        self.assertNotEqual(jobs[0]["lease_token"], jobs[1]["lease_token"])

    def test_same_session_is_exclusive_across_processors(self):
        for _ in range(4):
            self.raw("a")
        jobs = self.race([{"max_entries": 1}, {"max_entries": 1, "processor": "other"}])
        self.assertEqual(1, sum(job is not None for job in jobs))
        self.assertIsNone(self.claim(processor="third"))

    def test_serial_default_and_parallel_race_remain_exclusive(self):
        self.raw("a")
        self.raw("b")
        jobs = self.race([{"parallel_sessions": False}, {}])
        self.assertEqual(1, sum(job is not None for job in jobs))

    def test_parallel_cannot_bypass_live_serial_lease(self):
        self.raw("a")
        serial = self.claim(parallel_sessions=False)
        self.raw("b")
        self.assertIsNone(self.claim())
        self.fail(serial)
        self.assertIsNotNone(self.claim())

    def test_serial_cannot_bypass_live_parallel_lease(self):
        self.raw("a")
        self.claim()
        self.raw("b")
        self.assertIsNone(self.claim(parallel_sessions=False))
        self.assertIsNotNone(self.claim())

    def test_busy_session_excluded_before_candidate_limit(self):
        self.raw("busy")
        self.claim(max_entries=1)
        for _ in range(105):
            self.raw("busy")
        other = self.raw("other")
        last = self.raw("last")
        claimed = self.claim()
        self.assertEqual([other["id"]], [row["id"] for row in claimed["sources"]])
        self.assertEqual([last["id"]], [row["id"] for row in self.claim()["sources"]])

    def test_parallel_candidate_plan_does_not_rescan_project_jobs(self):
        self.raw("busy")
        self.claim()
        self.raw("busy")
        other = self.raw("other")
        statements = []
        self.store._connection.set_trace_callback(statements.append)
        try:
            claimed = self.claim()
        finally:
            self.store._connection.set_trace_callback(None)
        self.assertEqual([other["id"]], [row["id"] for row in claimed["sources"]])
        query = next(sql for sql in statements if "SELECT e.* FROM entries AS e" in sql)
        plan = [row[3] for row in self.store._connection.execute("EXPLAIN QUERY PLAN " + query)]
        self.assertTrue(any("observation_job_sources_source_idx (source_id=?)" in row for row in plan))
        self.assertFalse(any("observation_jobs_project_status_idx" in row for row in plan))
        snapshots = [sql for sql in statements
                     if "SELECT id, session_id, lease_token, lease_expires_at FROM observation_jobs" in sql]
        self.assertEqual(1, len(snapshots))

    def test_stop_cannot_overtake_active_sources(self):
        first = self.raw("a")
        running = self.claim(max_entries=1)
        stop = self.raw("a", source="hook:Stop")
        self.assertIsNone(self.claim())
        self.expire(running)
        recovered = self.claim()
        self.assertEqual(running["job_id"], recovered["job_id"])
        self.assertEqual([first["id"]], [row["id"] for row in recovered["sources"]])
        self.store.finish_observation_batch(
            self.project, recovered["job_id"], recovered["lease_token"], disposition="skipped",
        )
        self.assertEqual([stop["id"]], [row["id"] for row in self.claim()["sources"]])

    def test_stop_bounds_batch_before_next_turn(self):
        first = self.raw("a")
        stop = self.raw("a", source="hook:Stop")
        later = self.raw("a")
        job = self.claim()
        self.assertEqual([first["id"], stop["id"]], [row["id"] for row in job["sources"]])
        self.assertNotIn(later["id"], [row["id"] for row in job["sources"]])
        self.assertIsNone(self.claim())

    def test_expired_session_recovers_while_other_session_is_active(self):
        self.raw("a")
        first = self.claim()
        self.expire(first)
        self.raw("b")
        with Store(self.data) as other:
            second = self.claim(store=other, processor="other")
        self.assertEqual("b", second["session_id"])
        self.raw("a", source="hook:Stop")
        recovered = self.claim()
        self.assertEqual(first["job_id"], recovered["job_id"])
        self.assertEqual(2, recovered["attempt_count"])
        self.assertIsNone(self.claim())

    def test_expired_other_processor_blocks_its_session_only(self):
        self.raw("a")
        first = self.claim(processor="other")
        self.expire(first)
        self.raw("a", source="hook:Stop")
        other = self.raw("b")
        self.assertEqual([other["id"]], [row["id"] for row in self.claim()["sources"]])
        self.assertIsNone(self.claim())

    def test_expired_recovery_race_takes_one_new_lease(self):
        self.raw("a")
        original = self.claim()
        self.expire(original)
        self.raw("a", source="hook:Stop")
        jobs = self.race([{}, {}])
        claimed = [job for job in jobs if job is not None]
        self.assertEqual(1, len(claimed))
        self.assertEqual(original["job_id"], claimed[0]["job_id"])
        self.assertEqual(2, claimed[0]["attempt_count"])

    def test_unguarded_exact_retry_cannot_bypass_session_lease(self):
        self.raw("a")
        failed = self.claim()
        self.fail(failed)
        self.raw("a")
        self.claim()
        self.assertIsNone(self.claim(retry_job_id=failed["job_id"]))
        other = self.raw("b")
        fallback = self.claim(retry_job_id=failed["job_id"])
        self.assertEqual([other["id"]], [row["id"] for row in fallback["sources"]])

    def test_guarded_retry_can_run_beside_another_session(self):
        self.raw("a")
        first = self.claim()
        self.fail(first)
        self.raw("b")
        self.claim()
        recovered = self.claim(retry_job_id=first["job_id"], retry_error_code="timeout",
                               retry_attempt_count=1)
        self.assertEqual(first["job_id"], recovered["job_id"])
        self.assertEqual(2, recovered["attempt_count"])
        self.assertIsNone(self.claim(retry_job_id=first["job_id"], retry_error_code="timeout",
                                     retry_attempt_count=1))

    def test_exact_and_broad_retries_cannot_bypass_same_session(self):
        self.raw("a")
        first = self.claim()
        self.fail(first)
        self.raw("a")
        active = self.claim()
        self.raw("b")
        self.assertIsNone(self.claim(retry_job_id=first["job_id"], retry_error_code="timeout",
                                     retry_attempt_count=1))
        other = self.claim(retry_failed=True)
        self.assertEqual("b", other["session_id"])
        self.assertNotEqual(first["job_id"], other["job_id"])
        self.expire(active)
        recovered = self.claim(retry_failed=True)
        self.assertEqual(active["job_id"], recovered["job_id"])
        self.assertIsNone(self.claim(retry_failed=True))

    def test_broad_retry_allows_other_session_and_preserves_snapshot(self):
        original = self.raw("a")
        first = self.claim()
        self.fail(first)
        self.raw("b")
        self.claim()
        self.raw("a", source="hook:Stop")
        recovered = self.claim(retry_failed=True)
        self.assertEqual(first["job_id"], recovered["job_id"])
        self.assertEqual([original["id"]], [row["id"] for row in recovered["sources"]])

    def test_null_session_is_project_exclusive(self):
        self.raw(None)
        null = self.claim()
        self.raw("known")
        self.raw(None)
        self.assertIsNone(self.claim())
        self.expire(null)
        recovered = self.claim()
        self.assertEqual(null["job_id"], recovered["job_id"])
        self.assertIsNone(self.claim())

    def test_null_session_waits_for_known_session(self):
        self.raw("known")
        running = self.claim()
        self.raw(None)
        self.raw("other")
        next_job = self.claim()
        self.assertEqual("other", next_job["session_id"])
        self.assertIsNone(self.claim())
        self.fail(running)
        self.fail(next_job)
        self.assertIsNone(self.claim()["session_id"])

    def test_parallel_retires_expired_profile_and_reclaims_whole_source(self):
        original = self.raw("a", body="Whole evidence " * 120)
        retired = self.claim()
        self.store._connection.execute(
            "UPDATE observation_jobs SET model='gpt-5.6-luna', input_fingerprint='legacy-profile' WHERE id=?",
            (retired["job_id"],),
        )
        self.raw("b")
        self.assertEqual("b", self.claim()["session_id"])
        self.expire(retired)
        replacement = self.claim(max_chars=1000)
        self.assertEqual([original["id"]], [row["id"] for row in replacement["sources"]])
        self.assertEqual(original["body"], replacement["sources"][0]["body"])
        row = self.store._connection.execute(
            "SELECT status, disposition FROM observation_jobs WHERE id=?", (retired["job_id"],),
        ).fetchone()
        self.assertEqual(("failed", "profile_retired"), tuple(row))

    def test_parallel_flag_requires_boolean(self):
        with self.assertRaises(ValueError):
            self.claim(parallel_sessions=1)


if __name__ == "__main__":
    unittest.main()
