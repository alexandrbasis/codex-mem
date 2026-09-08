from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem.service import (
    SERVICE_PID_FILENAME,
    SERVICE_STATE_FILENAME,
    _PidLock,
    _clear_owner,
    _record_owner,
    enqueue,
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

    def test_explicit_timeout_retry_is_bounded_and_marks_retry_failed(self) -> None:
        enqueue(self.first, self.data_dir, clock=self.clock)
        calls: list[bool] = []

        def processor(_project: str, **kwargs: object) -> dict[str, str]:
            calls.append(bool(kwargs["retry_failed"]))
            return {"status": "failed", "code": "timeout"} if len(calls) == 1 else {"status": "idle"}

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
        self.assertEqual([False, True], calls)
        self.assertEqual(0, service_status(self.data_dir, clock=self.clock)["queued_projects"])

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
