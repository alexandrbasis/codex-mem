from __future__ import annotations

import errno
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

from codex_mem import __version__
from codex_mem.config import configure
from codex_mem.processor import MODEL, PROCESSOR_ID, REASONING_EFFORT, process_pending
from codex_mem.store import MAX_LEASE_SECONDS, Store
from codex_mem.service import (
    SERVICE_PID_FILENAME,
    SERVICE_STATE_FILENAME,
    _PidLock,
    _clear_owner,
    _record_owner,
    enqueue,
    resume_pending,
    recover_expired,
    run_service,
    service_status,
    start_service,
    stop_service,
    ServiceAlreadyRunning,
)


class Clock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def sleep(self, delay: float) -> None:
        self.value += delay


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data_dir = self.root / "memory"
        self.first = self.root / "first"
        self.second = self.root / "second"
        self.outside = self.root / "outside"
        for project in (self.first, self.second, self.outside):
            project.mkdir()
        configure(
            self.data_dir,
            capture_scope="selected",
            included_projects=[self.first, self.second],
        )
        self.clock = Clock()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_enqueue_is_explicit_and_selected_scope_only(self) -> None:
        rejected = enqueue(self.outside, self.data_dir, clock=self.clock)
        self.assertEqual("disabled", rejected["status"])
        self.assertFalse((self.data_dir / SERVICE_STATE_FILENAME).exists())

        accepted = enqueue(self.first, self.data_dir, clock=self.clock)
        self.assertEqual("queued", accepted["status"])
        state = json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())
        self.assertEqual([str(self.first.resolve())], list(state["projects"]))
        self.assertNotIn("body", json.dumps(state))
        self.assertNotIn("observation", json.dumps(state))

    def test_usage_collection_runs_while_idle_and_failure_does_not_block_queue(self) -> None:
        calls = []
        def collect(**kwargs):
            calls.append(kwargs)
            raise OSError("usage source unavailable")
        result = run_service(
            self.data_dir, processor=lambda *a, **k: {"status": "idle"},
            usage_collector=collect, clock=self.clock, sleeper=self.clock.sleep,
            poll_interval=1, max_cycles=32,
        )
        self.assertEqual("cycle_limit", result["status"])
        self.assertEqual(2, len(calls))
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["blocked_projects"])

    def test_observer_receipts_are_reconciled_on_startup_and_while_idle(self) -> None:
        with mock.patch("codex_mem.service._reconcile_observer_usage") as reconcile:
            result = run_service(
                self.data_dir, processor=lambda *a, **k: {"status": "idle"},
                clock=self.clock, sleeper=self.clock.sleep, poll_interval=1, max_cycles=32,
            )
        self.assertEqual("cycle_limit", result["status"])
        self.assertEqual(2, reconcile.call_count)
        self.assertEqual(self.data_dir.resolve(), reconcile.call_args.args[0])

    def test_disabled_service_does_not_reconcile_observer_receipts(self) -> None:
        configure(self.data_dir, service_enabled=False)
        with mock.patch("codex_mem.service._reconcile_observer_usage") as reconcile:
            result = run_service(self.data_dir, clock=self.clock, sleeper=self.clock.sleep, max_cycles=1)
        self.assertEqual("paused", result["status"])
        reconcile.assert_not_called()

    def test_usage_collection_respects_disable_without_disabling_memory(self) -> None:
        configure(self.data_dir, usage_enabled=False)
        enqueue(self.first, self.data_dir, clock=self.clock)
        calls = []
        result = run_service(
            self.data_dir, processor=lambda *a, **k: {"status": "idle"},
            usage_collector=lambda **kwargs: calls.append(kwargs),
            clock=self.clock, sleeper=self.clock.sleep, max_cycles=1,
        )
        self.assertEqual("cycle_limit", result["status"])
        self.assertEqual([], calls)
        self.assertEqual(1, result["jobs"])

    def test_enqueue_respects_service_enabled_and_the_configured_all_scope(self) -> None:
        configure(self.data_dir, service_enabled=False)
        disabled = enqueue(self.first, self.data_dir, clock=self.clock)
        self.assertEqual({"status": "disabled", "reason": "service_disabled"}, {
            "status": disabled["status"],
            "reason": disabled["reason"],
        })

        configure(self.data_dir, service_enabled=True, capture_scope="all")
        self.assertEqual("queued", enqueue(self.outside, self.data_dir, clock=self.clock)["status"])

    def test_queue_drains_fairly_and_processed_requeues_until_idle(self) -> None:
        enqueue(self.first, self.data_dir, clock=self.clock)
        enqueue(self.second, self.data_dir, clock=self.clock)
        calls: list[tuple[str, bool]] = []
        per_project: dict[str, int] = {}

        def processor(project: str, **kwargs: object) -> dict[str, str]:
            calls.append((project, bool(kwargs["retry_failed"])))
            count = per_project.get(project, 0)
            per_project[project] = count + 1
            return {"status": "processed" if count == 0 else "idle"}

        result = run_service(
            self.data_dir,
            processor=processor,
            clock=self.clock,
            sleeper=self.clock.sleep,
            poll_interval=0,
            max_cycles=4,
        )

        self.assertEqual("cycle_limit", result["status"])
        self.assertEqual(
            [str(self.first.resolve()), str(self.second.resolve()), str(self.first.resolve()), str(self.second.resolve())],
            [project for project, _ in calls],
        )
        self.assertTrue(all(not retry for _, retry in calls))
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["queued_projects"])

    def test_default_timeout_failure_blocks_and_needs_explicit_retry(self) -> None:
        enqueue(self.first, self.data_dir, clock=self.clock)
        result = run_service(
            self.data_dir,
            processor=lambda *_args, **_kwargs: {"status": "failed", "code": "timeout"},
            clock=self.clock,
            sleeper=self.clock.sleep,
            max_cycles=1,
        )
        self.assertEqual({"status": "halted", "code": "timeout"}, {
            "status": result["status"],
            "code": result["code"],
        })
        self.assertEqual(1, service_status(self.data_dir, clock=self.clock)["blocked_projects"])
        self.assertEqual("blocked", enqueue(self.first, self.data_dir, clock=self.clock)["status"])
        self.assertEqual("queued", enqueue(self.first, self.data_dir, retry_failed=True, clock=self.clock)["status"])

        retry_flags: list[bool] = []

        def recovered(_project: str, **kwargs: object) -> dict[str, str]:
            retry_flags.append(bool(kwargs["retry_failed"]))
            return {"status": "idle"}

        recovered_result = run_service(
            self.data_dir,
            processor=recovered,
            clock=self.clock,
            sleeper=self.clock.sleep,
            max_cycles=1,
        )
        self.assertEqual("cycle_limit", recovered_result["status"])
        self.assertEqual([True], retry_flags)
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["queued_projects"])

    def test_timeout_retry_is_bounded_and_selects_only_the_failed_job(self) -> None:
        enqueue(self.first, self.data_dir, clock=self.clock)
        calls = []

        def processor(_project: str, **kwargs: object) -> dict[str, str]:
            calls.append((kwargs["retry_failed"], kwargs.get("retry_job_id")))
            return ({"status": "failed", "code": "timeout", "job_id": "a" * 32}
                    if len(calls) == 1 else {"status": "idle"})

        result = run_service(
            self.data_dir,
            processor=processor,
            clock=self.clock,
            sleeper=self.clock.sleep,
            poll_interval=5,
            retry_backoff=5,
            max_timeout_retries=1,
            max_cycles=3,
        )
        self.assertEqual("cycle_limit", result["status"])
        self.assertEqual([(False, None), (False, "a" * 32)], calls)
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["queued_projects"])

    def test_timeout_without_exact_job_cannot_authorize_automatic_retry(self) -> None:
        enqueue(self.first, self.data_dir, clock=self.clock)
        processor = mock.Mock(return_value={"status": "failed", "code": "timeout"})
        result = run_service(self.data_dir, processor=processor, clock=self.clock,
                             sleeper=self.clock.sleep, max_timeout_retries=1, max_cycles=5)
        self.assertEqual("halted", result["status"])
        self.assertEqual(1, processor.call_count)

    def test_deferred_receipt_keeps_retry_intent_and_clamps_wait(self) -> None:
        for retry_at, expected_delay in ((1.0, 1.0), (10**12, MAX_LEASE_SECONDS)):
            with self.subTest(retry_at=retry_at):
                enqueue(self.first, self.data_dir, retry_failed=True, clock=self.clock)
                result = run_service(self.data_dir,
                    processor=lambda *_a, **_k: {"status": "deferred", "retry_at": retry_at},
                    clock=self.clock, sleeper=self.clock.sleep, max_cycles=1)
                self.assertEqual("cycle_limit", result["status"])
                state = json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())
                record = state["projects"][str(self.first.resolve())]
                self.assertTrue(record["retry_requested"])
                self.assertEqual(self.clock() + expected_delay, record["due_at"])
                calls = []
                run_service(self.data_dir, processor=lambda *a, **k: calls.append(k),
                            clock=self.clock, sleeper=self.clock.sleep, poll_interval=0, max_cycles=3)
                self.assertEqual([], calls)

    def test_malformed_deferred_receipt_fails_without_persisting_unsafe_timing(self) -> None:
        for retry_at in (None, True, "private-source-content", float("nan"), float("inf")):
            with self.subTest(retry_at=retry_at):
                enqueue(self.first, self.data_dir, retry_failed=True, clock=self.clock)
                result = run_service(self.data_dir,
                    processor=lambda *_a, **_k: {"status": "deferred", "retry_at": retry_at},
                    clock=self.clock, sleeper=self.clock.sleep, max_cycles=1)
                self.assertEqual("invalid_result", result["code"])
                self.assertNotIn("private-source-content", (self.data_dir / SERVICE_STATE_FILENAME).read_text())

    def test_legacy_retry_record_remains_readable_and_consumed(self) -> None:
        enqueue(self.first, self.data_dir, retry_failed=True, clock=self.clock)
        state_path = self.data_dir / SERVICE_STATE_FILENAME
        state = json.loads(state_path.read_text())
        record = state["projects"][str(self.first.resolve())]
        del record["retry_generation"]
        del record["retry_job_id"]
        state_path.write_text(json.dumps(state))
        flags = []

        def processor(_project, **kwargs):
            flags.append(kwargs["retry_failed"])
            return {"status": "idle"}

        run_service(self.data_dir, processor=processor, clock=self.clock,
                    sleeper=self.clock.sleep, max_cycles=1)
        self.assertEqual([True], flags)
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["queued_projects"])

    def test_retry_requested_during_processor_completion_reaches_next_invocation(self) -> None:
        rejected = self._failed_job(self.first)
        outcomes = [
            {"status": "idle"}, {"status": "processed"}, {"status": "skipped"},
            {"status": "failed", "code": "timeout"},
            {"status": "failed", "code": "storage_failure"},
            {"status": "failed", "code": "invalid_response", "job_id": rejected["job_id"],
             "reason_code": "invalid_output_shape"},
        ]
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                enqueue(self.first, self.data_dir, clock=self.clock)
                flags = []

                def processor(_project, **kwargs):
                    flags.append(kwargs["retry_failed"])
                    if len(flags) == 1:
                        enqueue(self.first, self.data_dir, retry_failed=True, clock=self.clock)
                        return outcome
                    return {"status": "idle"}

                run_service(self.data_dir, processor=processor, clock=self.clock,
                            sleeper=self.clock.sleep, poll_interval=0, max_cycles=2)
                self.assertEqual([False, True], flags)

    def test_ordinary_enqueue_does_not_repeat_an_already_consumed_explicit_retry(self) -> None:
        enqueue(self.first, self.data_dir, retry_failed=True, clock=self.clock)
        flags = []

        def processor(_project, **kwargs):
            flags.append(kwargs["retry_failed"])
            if len(flags) == 1:
                enqueue(self.first, self.data_dir, clock=self.clock)
            return {"status": "idle"}

        run_service(self.data_dir, processor=processor, clock=self.clock,
                    sleeper=self.clock.sleep, poll_interval=0, max_cycles=2)
        self.assertEqual([True, False], flags)

    def test_running_store_lease_survives_service_restart_and_recovers_once_due(self) -> None:
        self.clock.value = time.time()
        with Store(self.data_dir) as store:
            store.remember(self.first, "Leased source", "Retain unfinished evidence",
                           source="hook:PostToolUse", session_id="leased-session")
            job = store.claim_observation_batch(self.first, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                                                lease_seconds=3)
            expiry = datetime.fromisoformat(job["lease_expires_at"].replace("Z", "+00:00")).timestamp()
        runner = mock.Mock(return_value={
            "output": {"notes": [], "disposition": "skipped"},
            "evidence": {
                "thread_start": {"thread_id": "worker", "model": MODEL,
                                 "reasoning_effort": REASONING_EFFORT, "model_provider": "openai"},
                "turn_started": {"thread_id": "worker", "turn_id": "turn"},
                "turn_completed": True, "no_tools": True, "rerouted": False,
            },
        })
        receipts = []

        def processor(project, **kwargs):
            receipt = process_pending(project, runner=runner, **kwargs)
            receipts.append(receipt)
            return receipt

        def utc_now():
            return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(
                timespec="microseconds").replace("+00:00", "Z")

        enqueue(self.first, self.data_dir, clock=self.clock)
        with mock.patch("codex_mem.store._utc_now", side_effect=utc_now):
            run_service(self.data_dir, processor=processor, clock=self.clock,
                        sleeper=self.clock.sleep, max_cycles=1)
            self.assertEqual("deferred", receipts[0]["status"])
            self.assertEqual(0, runner.call_count)
            state = json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())
            self.assertEqual(expiry, state["projects"][str(self.first.resolve())]["due_at"])
            self.assertEqual(1, service_status(self.data_dir, clock=self.clock)["queued_projects"])
            run_service(self.data_dir, processor=processor, clock=self.clock,
                        sleeper=self.clock.sleep, poll_interval=5, max_cycles=3)
        self.assertEqual(["deferred", "skipped", "idle"], [item["status"] for item in receipts])
        self.assertEqual(1, runner.call_count)
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["queued_projects"])
        with Store(self.data_dir) as store:
            row = store._connection.execute("SELECT status,attempt_count FROM observation_jobs WHERE id=?",
                                             (job["job_id"],)).fetchone()
            self.assertEqual(("skipped", 2), tuple(row))

    def test_expired_lease_recovery_is_bounded_and_does_not_retry_rejections(self) -> None:
        enqueue(self.first, self.data_dir, clock=self.clock)
        calls = []

        def processor(_project: str, **kwargs: object) -> dict[str, str]:
            calls.append((self.clock(), kwargs["retry_failed"]))
            return {"status": "failed", "code": "lease_expired"}

        result = run_service(self.data_dir, processor=processor, clock=self.clock,
                             sleeper=self.clock.sleep, poll_interval=5, retry_backoff=5,
                             max_cycles=20)
        self.assertEqual("halted", result["status"])
        self.assertEqual([(1000.0, False), (1005.0, False), (1015.0, False)], calls)
        self.assertEqual(1, service_status(self.data_dir, clock=self.clock)["blocked_projects"])

    def test_explicit_retry_also_reaches_the_indexer(self) -> None:
        enqueue(self.first, self.data_dir, clock=self.clock)
        run_service(
            self.data_dir,
            processor=lambda *_args, **_kwargs: {"status": "failed", "code": "timeout"},
            clock=self.clock,
            sleeper=self.clock.sleep,
            max_cycles=1,
        )
        enqueue(self.first, self.data_dir, retry_failed=True, clock=self.clock)
        index_retries: list[bool] = []

        def indexer(_project: str, _data_dir: object, *, retry_failed: bool = False) -> dict[str, object]:
            index_retries.append(retry_failed)
            return {"status": "idle", "pending": 0}

        run_service(
            self.data_dir,
            processor=lambda *_args, **_kwargs: {"status": "idle"},
            indexer=indexer,
            clock=self.clock,
            sleeper=self.clock.sleep,
            max_cycles=1,
        )
        self.assertEqual([True], index_retries)

    def test_explicit_retry_reaches_new_and_existing_queue_records(self) -> None:
        # A Store failure can predate the durable service queue, so an
        # explicit retry on a newly-created record must reach both workers.
        enqueue(self.first, self.data_dir, retry_failed=True, clock=self.clock)
        enqueue(self.second, self.data_dir, clock=self.clock)
        enqueue(self.second, self.data_dir, retry_failed=True, clock=self.clock)
        processor_retries: list[bool] = []
        index_retries: list[bool] = []

        def processor(_project: str, **kwargs: object) -> dict[str, str]:
            processor_retries.append(bool(kwargs["retry_failed"]))
            return {"status": "idle"}

        def indexer(_project: str, _data_dir: object, *, retry_failed: bool = False) -> dict[str, object]:
            index_retries.append(retry_failed)
            return {"status": "idle", "pending": 0}

        run_service(
            self.data_dir,
            processor=processor,
            indexer=indexer,
            clock=self.clock,
            sleeper=self.clock.sleep,
            max_cycles=2,
        )
        self.assertEqual([True, True], processor_retries)
        self.assertEqual([True, True], index_retries)

    def test_nontransient_failure_never_auto_retries(self) -> None:
        enqueue(self.first, self.data_dir, clock=self.clock)
        calls = 0

        def processor(_project: str, **_kwargs: object) -> dict[str, str]:
            nonlocal calls
            calls += 1
            return {"status": "failed", "code": "invalid_response"}

        result = run_service(
            self.data_dir,
            processor=processor,
            clock=self.clock,
            sleeper=self.clock.sleep,
            max_timeout_retries=5,
            max_cycles=3,
        )
        self.assertEqual("halted", result["status"])
        self.assertEqual("invalid_response", result["code"])
        self.assertEqual(1, calls)

    def test_rejected_batch_does_not_starve_later_work_or_spin_when_idle(self) -> None:
        with Store(self.data_dir) as store:
            bad_source = store.remember(self.first, "Rejected source", "Earlier malformed output",
                                        source="hook:PostToolUse", session_id="earlier-chat")
            store.remember(self.first, "Useful later source", "Retry retains the original request ID.",
                           source="hook:PostToolUse", session_id="later-chat")
        enqueue(self.first, self.data_dir, clock=self.clock)
        calls: list[bool] = []
        worker_calls: list[str] = []

        def runner(request: dict[str, object]) -> dict[str, object]:
            source = request["sources"][0]
            worker_calls.append(source["title"])
            if source["title"] == "Rejected source":
                output = {"notes": "malformed", "disposition": "processed"}
            else:
                output = {"notes": [{"title": "Retry invariant", "body": "Retry retains the original request ID.",
                                     "tags": ["retry"], "source_ids": ["s1"]}],
                          "disposition": "processed", "session_summary": None}
            return {"output": output, "evidence": {
                "thread_start": {"thread_id": "test-thread", "model": MODEL,
                                 "reasoning_effort": REASONING_EFFORT, "model_provider": "openai"},
                "turn_started": {"thread_id": "test-thread", "turn_id": "test-turn"},
                "turn_completed": True, "no_tools": True, "rerouted": False}}

        def processor(project: str, **kwargs: object) -> dict[str, object]:
            calls.append(bool(kwargs["retry_failed"]))
            return process_pending(project, runner=runner, **kwargs)

        result = run_service(self.data_dir, processor=processor, clock=self.clock,
                             sleeper=self.clock.sleep, poll_interval=1, max_cycles=8)
        self.assertEqual("cycle_limit", result["status"])
        self.assertEqual("invalid_response", result["code"])
        self.assertEqual([False, False, False], calls)
        self.assertEqual(["Rejected source", "Useful later source"], worker_calls)
        with Store(self.data_dir) as store:
            jobs = store.status(self.first)["observation_jobs"]
            self.assertEqual(1, jobs["failed"])
            self.assertEqual(1, jobs["processed"])
            failed = next(job for job in jobs["recent"] if job["status"] == "failed")
            self.assertEqual(1, failed["attempt_count"])
            self.assertIsNone(store.get(self.first, [bad_source["id"]])[0]["superseded_by"])
            self.assertEqual("Retry invariant", store.search(self.first, "Retry")[0]["title"])
        status = service_status(self.data_dir, clock=self.clock)
        self.assertEqual(0, status["blocked_projects"])
        self.assertEqual(1, status["quarantined_projects"])
        self.assertEqual(1, status["quarantined_batches"])
        self.assertEqual("invalid_note_shape", status["quarantines"][str(self.first.resolve())]["reason_code"])
        # A new enqueue checks new work; it must never rearm the rejected job.
        self.assertEqual("queued", enqueue(self.first, self.data_dir, clock=self.clock)["status"])
        run_service(self.data_dir, processor=processor, clock=self.clock,
                    sleeper=self.clock.sleep, max_cycles=3)
        self.assertEqual([False, False, False, False], calls)
        self.assertEqual(2, len(worker_calls))

    def test_nontransient_failure_isolated_from_other_queued_projects(self) -> None:
        enqueue(self.first, self.data_dir, clock=self.clock)
        enqueue(self.second, self.data_dir, clock=self.clock)
        calls: list[tuple[str, bool]] = []

        def processor(project: str, **kwargs: object) -> dict[str, str]:
            calls.append((project, bool(kwargs["retry_failed"])))
            if project == str(self.first.resolve()):
                return {"status": "failed", "code": "invalid_response"}
            return {"status": "idle"}

        result = run_service(
            self.data_dir,
            processor=processor,
            clock=self.clock,
            sleeper=self.clock.sleep,
            max_cycles=2,
        )

        self.assertEqual("cycle_limit", result["status"])
        self.assertEqual(
            [str(self.first.resolve()), str(self.second.resolve())],
            [project for project, _ in calls],
        )
        self.assertEqual([False, False], [retry for _, retry in calls])
        state = json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())
        self.assertTrue(state["projects"][str(self.first.resolve())]["blocked"])
        self.assertNotIn(str(self.second.resolve()), state["projects"])
        self.assertEqual("blocked", enqueue(self.first, self.data_dir, clock=self.clock)["status"])

    def _failed_job(self, project: Path, code: str = "invalid_response") -> dict[str, object]:
        with Store(self.data_dir) as store:
            store.remember(project, "Raw source", "Preserve this evidence unchanged.",
                           source="hook:PostToolUse", session_id="failed-chat")
            job = store.claim_observation_batch(project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            return store.fail_observation_batch(project, job["job_id"], job["lease_token"], code=code)

    def test_recover_expired_validates_exact_claim_and_preserves_quarantine(self) -> None:
        failed = self._failed_job(self.first)
        with Store(self.data_dir) as store:
            store.remember(self.first, "New source", "Independent pending evidence.",
                           source="hook:PostToolUse", session_id="new-chat")
            job = store.claim_observation_batch(self.first, PROCESSOR_ID, MODEL, REASONING_EFFORT)
        enqueue(self.first, self.data_dir, clock=self.clock)
        run_service(self.data_dir, processor=lambda *_a, **_k: {"status": "failed", "code": "storage_failure"},
                    clock=self.clock, sleeper=self.clock.sleep, max_cycles=1)
        for rejected_id in ("missing", failed["job_id"], job["job_id"]):
            self.assertEqual("expired_claim_unavailable", recover_expired(
                self.first, self.data_dir, job_id=rejected_id, clock=self.clock)["code"])
        with Store(self.data_dir) as store:
            store._connection.execute("UPDATE observation_jobs SET lease_expires_at = ? WHERE id = ?",
                                      ("1970-01-01T00:01:00Z", job["job_id"]))
        self.assertEqual("blocker_unavailable", recover_expired(
            self.second, self.data_dir, job_id=job["job_id"], clock=self.clock)["code"])
        state = json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())
        state["projects"][str(self.first.resolve())]["last_code"] = "tools_available"
        (self.data_dir / SERVICE_STATE_FILENAME).write_text(json.dumps(state))
        self.assertEqual("tools_available", recover_expired(
            self.first, self.data_dir, job_id=job["job_id"], clock=self.clock)["code"])
        state["projects"][str(self.first.resolve())]["last_code"] = "storage_failure"
        (self.data_dir / SERVICE_STATE_FILENAME).write_text(json.dumps(state))
        result = recover_expired(self.first, self.data_dir, job_id=job["job_id"], clock=self.clock)
        self.assertEqual("queued", result["status"])
        self.assertFalse(result["retry_failed"])
        with Store(self.data_dir) as store:
            rows = store._connection.execute("SELECT status,attempt_count FROM observation_jobs WHERE id = ?",
                                             (failed["job_id"],)).fetchone()
            self.assertEqual(("failed", 1), tuple(rows))
        self.assertEqual("blocker_unavailable", recover_expired(
            self.first, self.data_dir, job_id=job["job_id"], clock=self.clock)["code"])

    def test_resume_pending_recovers_legacy_block_without_retrying_failed_job(self) -> None:
        failed = self._failed_job(self.first)
        enqueue(self.first, self.data_dir, clock=self.clock)
        # A legacy scheduler blocked the project without retaining a job ID.
        run_service(self.data_dir, processor=lambda *_a, **_k: {"status": "failed", "code": "invalid_response"},
                    clock=self.clock, sleeper=self.clock.sleep, max_cycles=1)
        recovered = resume_pending(self.first, self.data_dir, rejected_job_id=failed["job_id"], clock=self.clock)
        self.assertEqual("queued", recovered["status"])
        self.assertFalse(recovered["retry_failed"])
        calls: list[bool] = []

        def processor(project: str, **kwargs: object) -> dict[str, object]:
            calls.append(bool(kwargs["retry_failed"]))
            return process_pending(project, **kwargs, runner=lambda _: self.fail("Failed job was rearmed"))

        run_service(self.data_dir, processor=processor, clock=self.clock,
                    sleeper=self.clock.sleep, max_cycles=5)
        self.assertEqual([False], calls)
        status = service_status(self.data_dir, clock=self.clock)
        self.assertEqual(0, status["blocked_projects"])
        self.assertEqual(1, status["quarantined_batches"])
        self.assertEqual(failed["job_id"], status["quarantines"][str(self.first.resolve())]["last_job_id"])
        with Store(self.data_dir) as store:
            actual = store.status(self.first)["observation_jobs"]["recent"][0]
            self.assertEqual("failed", actual["status"])
            self.assertEqual(1, actual["attempt_count"])

    def test_resume_pending_cannot_clear_foreign_missing_or_global_failures(self) -> None:
        first_job = self._failed_job(self.first)
        second_job = self._failed_job(self.second)
        for project, job_id in ((self.first, second_job["job_id"]), (self.second, "missing-job")):
            result = resume_pending(project, self.data_dir, rejected_job_id=job_id, clock=self.clock)
            self.assertEqual("rejection_unavailable", result["code"])
        self.assertFalse((self.data_dir / SERVICE_STATE_FILENAME).exists())
        for code in ("model_mismatch", "tools_available", "tool_called", "storage_failure", "protocol_error"):
            with self.subTest(code=code):
                enqueue(self.first, self.data_dir, retry_failed=True, clock=self.clock)
                result = run_service(self.data_dir,
                    processor=lambda *_a, **_k: {"status": "failed", "code": code, "job_id": first_job["job_id"]},
                    clock=self.clock, sleeper=self.clock.sleep, max_cycles=1)
                self.assertEqual("halted", result["status"])
                result = resume_pending(self.first, self.data_dir,
                                        rejected_job_id=first_job["job_id"], clock=self.clock)
                self.assertEqual({"status": "blocked", "code": code},
                                 {"status": result["status"], "code": result["code"]})

    def test_repeated_identical_rejection_receipt_does_not_spin(self) -> None:
        failed = self._failed_job(self.first)
        enqueue(self.first, self.data_dir, clock=self.clock)
        calls: list[bool] = []

        def processor(_project: str, **kwargs: object) -> dict[str, object]:
            calls.append(bool(kwargs["retry_failed"]))
            return {"status": "failed", "code": "invalid_response", "job_id": failed["job_id"],
                    "reason_code": "invalid_output_shape"}

        result = run_service(self.data_dir, processor=processor, clock=self.clock,
                             sleeper=self.clock.sleep, max_cycles=10)
        self.assertEqual("halted", result["status"])
        self.assertEqual([False, False], calls)
        self.assertEqual(1, service_status(self.data_dir, clock=self.clock)["quarantined_batches"])

    def test_unknown_and_runner_rejection_reasons_remain_blocked(self) -> None:
        failed = self._failed_job(self.first)
        for reason in (None, "invalid_runner_receipt", "invalid_runner_evidence", "worker_id_mismatch",
                       "turn_not_completed", "invalid_source_batch", "secret-response-excerpt"):
            with self.subTest(reason=reason):
                enqueue(self.first, self.data_dir, retry_failed=True, clock=self.clock)
                result = run_service(self.data_dir,
                    processor=lambda *_a, **_k: {"status": "failed", "code": "invalid_response",
                                                "job_id": failed["job_id"], "reason_code": reason},
                    clock=self.clock, sleeper=self.clock.sleep, max_cycles=4)
                self.assertEqual("halted", result["status"])
                self.assertEqual(1, result["jobs"])
                if reason in {"invalid_runner_receipt", "invalid_runner_evidence", "worker_id_mismatch",
                              "turn_not_completed", "invalid_source_batch"}:
                    state = json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())
                    self.assertEqual(reason, state["projects"][str(self.first.resolve())]["last_rejected_reason"])
                    recovery = resume_pending(self.first, self.data_dir,
                                              rejected_job_id=failed["job_id"], clock=self.clock)
                    self.assertEqual("blocked", recovery["status"])
                    self.assertEqual(reason, recovery["reason_code"])
        self.assertNotIn("secret-response-excerpt", (self.data_dir / SERVICE_STATE_FILENAME).read_text())

    def test_quarantine_health_clears_only_after_explicit_job_resolution(self) -> None:
        failed = self._failed_job(self.first)
        resume_pending(self.first, self.data_dir, rejected_job_id=failed["job_id"], clock=self.clock)
        self.assertEqual(1, service_status(self.data_dir, clock=self.clock)["quarantined_batches"])
        with Store(self.data_dir) as store:
            retry = store.claim_observation_batch(self.first, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                                                  retry_failed=True)
            self.assertEqual(failed["job_id"], retry["job_id"])
            store.finish_observation_batch(self.first, retry["job_id"], retry["lease_token"], disposition="skipped")
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["quarantined_batches"])
        run_service(self.data_dir, processor=lambda *_a, **_k: {"status": "idle"},
                    clock=self.clock, sleeper=self.clock.sleep, max_cycles=1)
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["queued_projects"])

    def test_capture_gate_is_rechecked_before_processing(self) -> None:
        enqueue(self.first, self.data_dir, clock=self.clock)
        configure(self.data_dir, capture_enabled=False)
        called = False

        def processor(*_args: object, **_kwargs: object) -> dict[str, str]:
            nonlocal called
            called = True
            return {"status": "idle"}

        result = run_service(
            self.data_dir,
            processor=processor,
            clock=self.clock,
            sleeper=self.clock.sleep,
            max_cycles=1,
        )
        self.assertEqual("paused", result["status"])
        self.assertFalse(called)
        self.assertEqual(1, service_status(self.data_dir, clock=self.clock)["queued_projects"])

    def test_emergency_environment_prevents_enqueue_and_work(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_MEM_DISABLED": "1"}):
            result = enqueue(self.first, self.data_dir, clock=self.clock)
        self.assertEqual("disabled", result["status"])

        enqueue(self.first, self.data_dir, clock=self.clock)
        with mock.patch.dict(os.environ, {"CODEX_MEM_DISABLED": "1"}):
            result = run_service(
                self.data_dir,
                processor=lambda *_args, **_kwargs: self.fail("processor must not run"),
                clock=self.clock,
                sleeper=self.clock.sleep,
                max_cycles=1,
            )
        self.assertEqual("paused", result["status"])

    def test_indexer_keeps_idle_project_queued_only_while_it_has_pending_work(self) -> None:
        enqueue(self.first, self.data_dir, clock=self.clock)
        indexing = [True, False]
        processor_calls = 0

        def processor(*_args: object, **_kwargs: object) -> dict[str, str]:
            nonlocal processor_calls
            processor_calls += 1
            return {"status": "idle"}

        def indexer(_project: str, _data_dir: object) -> bool:
            return indexing.pop(0)

        run_service(
            self.data_dir,
            processor=processor,
            indexer=indexer,
            clock=self.clock,
            sleeper=self.clock.sleep,
            max_cycles=2,
        )
        self.assertEqual(2, processor_calls)
        self.assertEqual([], indexing)
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["queued_projects"])

    def test_disabled_processor_still_runs_local_indexer_and_keeps_raw_work_queued(self) -> None:
        enqueue(self.first, self.data_dir, clock=self.clock)
        configure(self.data_dir, processor_enabled=False)
        indexed: list[str] = []

        def indexer(project: str, _data_dir: object) -> dict[str, object]:
            indexed.append(project)
            return {"status": "indexed", "pending": 0}

        result = run_service(
            self.data_dir,
            processor=lambda *_args, **_kwargs: self.fail("AI processor must not run"),
            indexer=indexer,
            clock=self.clock,
            sleeper=self.clock.sleep,
            max_cycles=1,
        )
        self.assertEqual("cycle_limit", result["status"])
        self.assertEqual([str(self.first.resolve())], indexed)
        self.assertEqual(1, service_status(self.data_dir, clock=self.clock)["queued_projects"])

    def test_disabled_processor_drains_local_indexing_fairly_without_spinning(self) -> None:
        enqueue(self.first, self.data_dir, clock=self.clock)
        enqueue(self.second, self.data_dir, clock=self.clock)
        configure(self.data_dir, processor_enabled=False)
        indexed: list[str] = []

        def indexer(project: str, _data_dir: object) -> dict[str, object]:
            indexed.append(project)
            return {"status": "indexed", "pending": 0}

        result = run_service(
            self.data_dir,
            processor=lambda *_args, **_kwargs: self.fail("AI processor must not run"),
            indexer=indexer,
            clock=self.clock,
            sleeper=self.clock.sleep,
            max_cycles=2,
        )
        self.assertEqual("cycle_limit", result["status"])
        self.assertEqual([str(self.first.resolve()), str(self.second.resolve())], indexed)
        state = json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())
        self.assertEqual({"processor_disabled"}, {entry["parked"] for entry in state["projects"].values()})

    def test_excluded_project_is_parked_without_starving_another_selected_project(self) -> None:
        enqueue(self.first, self.data_dir, clock=self.clock)
        enqueue(self.second, self.data_dir, clock=self.clock)
        configure(self.data_dir, included_projects=[self.second])
        calls: list[str] = []

        def processor(project: str, **_kwargs: object) -> dict[str, str]:
            calls.append(project)
            return {"status": "idle"}

        run_service(
            self.data_dir,
            processor=processor,
            clock=self.clock,
            sleeper=self.clock.sleep,
            max_cycles=2,
        )
        self.assertEqual([str(self.second.resolve())], calls)
        state = json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())
        self.assertEqual("not_selected", state["projects"][str(self.first.resolve())]["parked"])

    def test_index_failure_is_never_acknowledged_even_without_native_processing(self) -> None:
        enqueue(self.first, self.data_dir, clock=self.clock)
        configure(self.data_dir, processor_enabled=False)

        result = run_service(
            self.data_dir,
            processor=lambda *_args, **_kwargs: self.fail("AI processor must not run"),
            indexer=lambda *_args: {"status": "failed", "code": "embedding_failed", "pending": 0},
            clock=self.clock,
            sleeper=self.clock.sleep,
            max_cycles=1,
        )
        self.assertEqual({"status": "halted", "code": "index_failure"}, {
            "status": result["status"],
            "code": result["code"],
        })
        self.assertEqual(1, service_status(self.data_dir, clock=self.clock)["blocked_projects"])

    def test_stop_is_durable_and_never_signals_the_pid(self) -> None:
        lock = _PidLock(self.data_dir, self.clock())
        lock.acquire()
        try:
            _record_owner(self.data_dir, lock.pid, lock.nonce, self.clock())
            with mock.patch("codex_mem.service.os.kill", wraps=os.kill) as kill:
                stopped = stop_service(self.data_dir, clock=self.clock)
            self.assertEqual("stopping", stopped["status"])
            self.assertEqual([], [call for call in kill.call_args_list if call.args[1] != 0])
            state = json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())
            self.assertTrue(state["stop_requested"])
        finally:
            _clear_owner(self.data_dir, lock.pid, lock.nonce)
            lock.release()

    def test_runtime_identity_requires_verified_owner_and_accepts_legacy_metadata(self) -> None:
        lock = _PidLock(self.data_dir, self.clock())
        lock.acquire()
        try:
            _record_owner(self.data_dir, lock.pid, lock.nonce, self.clock())
            status = service_status(self.data_dir, clock=self.clock)
            self.assertTrue(status["lock_held"])
            self.assertEqual(f"{lock.pid}:{lock.nonce}", status["owner_id"])
            self.assertEqual(__version__, status["runtime_version"])
            state_path = self.data_dir / SERVICE_STATE_FILENAME
            state = json.loads(state_path.read_text())
            del state["owner"]["runtime_version"]
            state_path.write_text(json.dumps(state))
            legacy_status = service_status(self.data_dir, clock=self.clock)
            self.assertEqual("running", legacy_status["status"])
            self.assertIsNone(legacy_status["runtime_version"])
        finally:
            lock.release()
        stopped = service_status(self.data_dir, clock=self.clock)
        self.assertFalse(stopped["lock_held"])
        self.assertNotIn("runtime_version", stopped)
        self.assertNotIn("owner_id", stopped)

    def test_stop_with_changed_owner_cannot_stop_the_new_worker(self) -> None:
        lock = _PidLock(self.data_dir, self.clock())
        lock.acquire()
        try:
            _record_owner(self.data_dir, lock.pid, lock.nonce, self.clock())
            result = stop_service(self.data_dir, clock=self.clock, expected_owner="1234:" + "f" * 32)
            self.assertNotEqual("stopping", result["status"])
            state = json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())
            self.assertFalse(state["stop_requested"])
        finally:
            _clear_owner(self.data_dir, lock.pid, lock.nonce)
            lock.release()

    def test_guarded_stop_rechecks_owner_inside_the_state_lock(self) -> None:
        lock = _PidLock(self.data_dir, self.clock())
        lock.acquire()
        old_owner = "1234:" + "f" * 32
        try:
            _record_owner(self.data_dir, lock.pid, lock.nonce, self.clock())
            with mock.patch("codex_mem.service.service_status", return_value={
                "status": "running", "pid": 1234, "owner_id": old_owner,
            }):
                result = stop_service(self.data_dir, clock=self.clock, expected_owner=old_owner)
            self.assertEqual({"status": "blocked", "code": "owner_changed"}, result)
            state = json.loads((self.data_dir / SERVICE_STATE_FILENAME).read_text())
            self.assertFalse(state["stop_requested"])
            matched = stop_service(self.data_dir, clock=self.clock,
                                   expected_owner=f"{lock.pid}:{lock.nonce}")
            self.assertEqual("stopping", matched["status"])
        finally:
            _clear_owner(self.data_dir, lock.pid, lock.nonce)
            lock.release()

    def test_pid_flock_blocks_a_second_worker_during_partial_metadata_write(self) -> None:
        first = _PidLock(self.data_dir, self.clock())
        first.acquire()
        try:
            # Metadata is advisory; the retained flock is the single-instance
            # authority even if a concurrent reader sees an empty file.
            (self.data_dir / SERVICE_PID_FILENAME).write_text("", encoding="utf-8")
            second = _PidLock(self.data_dir, self.clock())
            with self.assertRaises(ServiceAlreadyRunning):
                second.acquire()
        finally:
            first.release()

    def test_status_does_not_trust_reused_live_pid_metadata_without_held_flock(self) -> None:
        nonce = "a" * 32
        _record_owner(self.data_dir, os.getpid(), nonce, self.clock())
        (self.data_dir / SERVICE_PID_FILENAME).write_text(
            json.dumps({"version": 1, "pid": os.getpid(), "nonce": nonce, "started_at": self.clock()}),
            encoding="utf-8",
        )
        status = service_status(self.data_dir, clock=self.clock)
        self.assertEqual("stopped", status["status"])
        self.assertFalse(status["running"])

    def test_status_uses_a_readonly_pid_lock_probe(self) -> None:
        lock = _PidLock(self.data_dir, self.clock())
        lock.acquire()
        try:
            _record_owner(self.data_dir, lock.pid, lock.nonce, self.clock())
            original_open = os.open

            def deny_readwrite(path: object, flags: int, *args: object) -> int:
                if flags & os.O_RDWR:
                    raise PermissionError("write access denied")
                return original_open(path, flags, *args)

            with mock.patch("codex_mem.service.os.open", side_effect=deny_readwrite):
                status = service_status(self.data_dir, clock=self.clock)
            self.assertEqual("running", status["status"])
            self.assertTrue(status["running"])
            self.assertEqual(lock.pid, status["pid"])
        finally:
            _clear_owner(self.data_dir, lock.pid, lock.nonce)
            lock.release()

    def test_denied_flock_probe_closes_its_temporary_descriptor(self) -> None:
        lock = _PidLock(self.data_dir, self.clock())
        lock.acquire()
        try:
            _record_owner(self.data_dir, lock.pid, lock.nonce, self.clock())
            probe_descriptors: list[int] = []

            def deny_flock(descriptor: int, _operation: int) -> None:
                probe_descriptors.append(descriptor)
                raise PermissionError(errno.EPERM, "lock visibility denied")

            with mock.patch("codex_mem.service.fcntl.flock", side_effect=deny_flock):
                status = service_status(self.data_dir, clock=self.clock)
            self.assertEqual("unknown", status["status"])
            self.assertIsNone(status["running"])
            self.assertEqual(1, len(probe_descriptors))
            with self.assertRaises(OSError):
                os.fstat(probe_descriptors[0])
        finally:
            _clear_owner(self.data_dir, lock.pid, lock.nonce)
            lock.release()

    def test_status_treats_permission_denied_pid_probe_as_alive(self) -> None:
        lock = _PidLock(self.data_dir, self.clock())
        lock.acquire()
        try:
            _record_owner(self.data_dir, lock.pid, lock.nonce, self.clock())
            with mock.patch(
                "codex_mem.service.os.kill",
                side_effect=PermissionError(errno.EPERM, "process visibility denied"),
            ):
                status = service_status(self.data_dir, clock=self.clock)
            self.assertEqual("running", status["status"])
            self.assertTrue(status["running"])
            self.assertEqual(lock.pid, status["pid"])
        finally:
            _clear_owner(self.data_dir, lock.pid, lock.nonce)
            lock.release()

    def test_denied_pid_lock_visibility_is_unknown_and_lifecycle_fails_closed(self) -> None:
        lock = _PidLock(self.data_dir, self.clock())
        lock.acquire()
        try:
            _record_owner(self.data_dir, lock.pid, lock.nonce, self.clock())
            state_path = self.data_dir / SERVICE_STATE_FILENAME
            before = state_path.read_bytes()
            launched: list[object] = []

            def launcher(*args: object) -> object:
                launched.append(args)
                return object()

            with mock.patch(
                "codex_mem.service.os.open", side_effect=PermissionError("lock access denied")
            ):
                status = service_status(self.data_dir, clock=self.clock)
                self.assertEqual("unknown", status["status"])
                self.assertIsNone(status["running"])
                self.assertEqual("lock_visibility_unavailable", status["code"])
                self.assertEqual(lock.pid, status["pid"])
                started = start_service(
                    self.data_dir,
                    launcher=launcher,
                    clock=self.clock,
                    sleeper=self.clock.sleep,
                    startup_timeout=0,
                )
                with mock.patch("codex_mem.service.os.kill", wraps=os.kill) as kill:
                    stopped = stop_service(self.data_dir, clock=self.clock)

            self.assertEqual(
                {"status": "unknown", "code": "lock_visibility_unavailable"}, started
            )
            self.assertEqual(
                {"status": "unknown", "code": "lock_visibility_unavailable"}, stopped
            )
            self.assertEqual([], launched)
            self.assertEqual([], kill.call_args_list)
            self.assertEqual(before, state_path.read_bytes())
        finally:
            _clear_owner(self.data_dir, lock.pid, lock.nonce)
            lock.release()

    def test_start_reservation_launches_once_without_a_real_child(self) -> None:
        launched: list[tuple[list[str], dict[str, str]]] = []

        class Child:
            pid = os.getpid()

            @staticmethod
            def poll() -> None:
                return None

        def launcher(command: list[str], environment: object) -> Child:
            launched.append((command, dict(environment)))
            return Child()

        first = start_service(
            self.data_dir,
            launcher=launcher,
            clock=self.clock,
            sleeper=self.clock.sleep,
            startup_timeout=0,
        )
        second = start_service(
            self.data_dir,
            launcher=launcher,
            clock=self.clock,
            sleeper=self.clock.sleep,
            startup_timeout=0,
        )
        self.assertEqual("starting", first["status"])
        self.assertEqual("already_starting", second["status"])
        self.assertEqual(1, len(launched))
        script = Path(__file__).resolve().parents[1] / "scripts" / "codex-mem.py"
        self.assertEqual([sys.executable, str(script), "--data-dir"], launched[0][0][:3])
        self.assertEqual(["service", "run"], launched[0][0][-2:])
        self.assertIn("--data-dir", launched[0][0])
        self.assertIn("CODEX_MEM_SERVICE_STARTUP_NONCE", launched[0][1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
