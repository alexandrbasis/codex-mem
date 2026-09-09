"""A small, local queue for bounded observation processing.

The service deliberately stores only project scheduling metadata.  Observation
text remains in :mod:`codex_mem.store`, where the processor leases it just
before a worker runs.  Hooks (or a future CLI) explicitly enqueue an approved
project; this module never discovers projects or starts itself at login.

``run_service`` is the foreground worker used by the packaged CLI.  The
``start_service`` helper starts one detached ``service run`` child, guarded by
an atomic startup reservation and a child-owned PID lock.  Stopping is a
durable request observed between bounded processor invocations; it never sends
signals to a PID that might have been reused by an unrelated process.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import errno
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

try:  # The supported host platforms provide flock; retain a stdlib fallback.
    import fcntl
except ImportError:  # pragma: no cover - Windows does not provide flock
    fcntl = None  # type: ignore[assignment]

from .config import automatic_capture_enabled, data_dir_path, hooks_disabled, load_config
from .processor import (
    DEFAULT_TIMEOUT, INVALID_RESPONSE_REASONS, MODEL, PROCESSOR_ID, REASONING_EFFORT, process_pending,
)
from .store import project_key


SERVICE_STATE_FILENAME = "service-state.json"
SERVICE_STATE_LOCK_FILENAME = ".service-state.lock"
SERVICE_PID_FILENAME = "service.pid"
SERVICE_STARTUP_FILENAME = ".service-starting.json"
SERVICE_STARTUP_ENV = "CODEX_MEM_SERVICE_STARTUP_NONCE"

STATE_VERSION = 1
MAX_STATE_BYTES = 256 * 1024
MAX_QUEUED_PROJECTS = 256
MAX_TIMEOUT_RETRIES = 5
DEFAULT_MAX_TIMEOUT_RETRIES = 0
DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_STARTUP_TIMEOUT = 3.0
DEFAULT_STARTUP_TTL = 15.0
DEFAULT_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 300.0
_INFLIGHT_GRACE_SECONDS = 10.0
_SAFE_CODE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
_PARK_REASONS = {"not_selected", "processor_disabled", "semantic_disabled", "rejected_batches"}
_CONTENT_REJECTION_REASONS = frozenset({
    "invalid_json", "invalid_output_shape", "invalid_note_shape", "invalid_source_ids",
    "unknown_source_handle", "invalid_disposition", "too_many_notes", "missing_required_summary",
    "skipped_with_content", "processed_without_content", "invalid_observation_metadata",
    "source_attribution_conflict", "invalid_summary_shape", "invalid_summary_attribution",
    "future_summary_source", "invalid_summary_text", "invalid_summary_metadata", "invalid_text", "invalid_tags",
})


class ServiceError(RuntimeError):
    """A stable, non-sensitive local service error."""


class ServiceAlreadyRunning(ServiceError):
    """Raised internally when a live service PID lock already exists."""


class ServiceStateError(ServiceError):
    """Raised when durable queue metadata is unavailable or malformed."""


def enqueue(
    project: str | os.PathLike[str],
    data_dir: str | os.PathLike[str] | None = None,
    *,
    retry_failed: bool = False,
    config_loader: Callable[[str | os.PathLike[str] | None], Mapping[str, Any]] = load_config,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Durably schedule one explicitly approved project.

    The queue holds no observation text or model output. A normal enqueue
    wakes a project with quarantined batches without retrying those batches.
    A project blocked by a global failure still requires explicit recovery.
    """

    if not isinstance(retry_failed, bool):
        raise ValueError("retry_failed must be true or false")
    workspace = project_key(project)
    eligible, reason, _, _ = _eligibility(workspace, data_dir, config_loader)
    if not eligible:
        return {"status": "disabled", "project": workspace, "reason": reason}

    now = _checked_now(clock)
    base = _base_dir(data_dir)
    with _state_lock(base):
        state = _load_state(base)
        projects = state["projects"]
        record = projects.get(workspace)
        if record is None:
            if len(projects) >= MAX_QUEUED_PROJECTS:
                raise ServiceError("service queue is full")
            # An explicit retry can be the first queue record created after a
            # Store-level observation or embedding failure.  Preserve the
            # caller's decision even though this service state has not itself
            # recorded a blocked failure yet.
            projects[workspace] = _new_record(now, retry_requested=retry_failed)
            _write_state(base, state)
            return {"status": "queued", "project": workspace}

        if record["blocked"] and not retry_failed:
            return {"status": "blocked", "project": workspace, "code": record["last_code"]}

        record["generation"] += 1
        record["due_at"] = min(record["due_at"], now)
        record["parked"] = None
        # Keep an explicit retry request distinct from the queue's local
        # failure state.  This permits recovery of a Store job that failed
        # outside an earlier service run.
        if retry_failed:
            record["retry_requested"] = True
        if record["blocked"]:
            record["blocked"] = False
            record["attempts"] = 0
            record["last_code"] = None
            # Store deliberately requires an explicit retry signal after a
            # failed leased observation job.  Keep that signal separate from
            # the service's timeout-backoff counter.
            record["retry_requested"] = True
        _write_state(base, state)
        return {"status": "queued", "project": workspace}


def resume_pending(
    project: str | os.PathLike[str],
    data_dir: str | os.PathLike[str] | None = None,
    *,
    rejected_job_id: str,
    config_loader: Callable[[str | os.PathLike[str] | None], Mapping[str, Any]] = load_config,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Resume independent work after inspecting an exact rejected batch.

    This recovers legacy project-wide invalid_response blocks. It never
    changes or retries the rejected Store job; retry_failed remains false.
    Other project failures cannot be cleared through this narrower operation.
    """

    workspace = project_key(project)
    eligible, reason, _, _ = _eligibility(workspace, data_dir, config_loader)
    if not eligible:
        return {"status": "disabled", "project": workspace, "reason": reason}
    base = _base_dir(data_dir)
    if not _persisted_rejection(base, workspace, rejected_job_id):
        return {"status": "blocked", "project": workspace, "code": "rejection_unavailable"}
    now = _checked_now(clock)
    with _state_lock(base):
        state = _load_state(base)
        record = state["projects"].get(workspace)
        if record is not None and record["blocked"]:
            reason_code = record["last_rejected_reason"]
            if (record["last_code"] != "invalid_response"
                    or (reason_code is not None and reason_code not in _CONTENT_REJECTION_REASONS)):
                return {"status": "blocked", "project": workspace, "code": record["last_code"],
                        "reason_code": reason_code}
        if record is None:
            if len(state["projects"]) >= MAX_QUEUED_PROJECTS:
                raise ServiceError("service queue is full")
            record = state["projects"][workspace] = _new_record(now)
        if record["inflight_generation"] is not None:
            return {"status": "blocked", "project": workspace, "code": "work_inflight"}
        _record_rejection(record, rejected_job_id)
        record["generation"] += 1
        record["blocked"] = False
        record["parked"] = None
        record["due_at"] = now
        record["attempts"] = 0
        record["retry_requested"] = False
        _write_state(base, state)
    return {"status": "queued", "project": workspace,
            "rejected_job_id": rejected_job_id, "retry_failed": False}


def run_service(
    data_dir: str | os.PathLike[str] | None = None,
    *,
    stop_event: Any | None = None,
    processor: Callable[..., Mapping[str, Any]] | None = None,
    runner: Callable[..., Mapping[str, Any]] | None = None,
    config_loader: Callable[[str | os.PathLike[str] | None], Mapping[str, Any]] = load_config,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], Any] = time.sleep,
    poll_interval: int | float = DEFAULT_POLL_INTERVAL,
    processor_timeout: int | float = DEFAULT_TIMEOUT,
    max_timeout_retries: int = DEFAULT_MAX_TIMEOUT_RETRIES,
    retry_backoff: int | float = DEFAULT_BACKOFF_SECONDS,
    indexer: Callable[..., Any] | None = None,
    max_cycles: int | None = None,
) -> dict[str, Any]:
    """Run one local worker until stopped.

    A running service is single-instance per data directory.  It uses the
    processor's own Store leases and processes one project batch at a time.
    ``runner`` is a backwards-friendly test seam alias for ``processor``;
    only one may be supplied.  With ``max_cycles=None`` this is a persistent
    foreground service.  Tests can supply a finite cycle count and fake clock.
    """

    active_processor = _choose_processor(processor, runner)
    checked_poll = _validate_nonnegative_number(poll_interval, "poll_interval")
    checked_timeout = _validate_timeout(processor_timeout)
    checked_retries = _validate_retries(max_timeout_retries)
    checked_backoff = _validate_positive_number(retry_backoff, "retry_backoff")
    checked_cycles = _validate_cycles(max_cycles)
    _validate_stop_event(stop_event)

    base = _base_dir(data_dir)
    pid_lock = _PidLock(base, _checked_now(clock))
    try:
        pid_lock.acquire()
    except ServiceAlreadyRunning:
        return _service_receipt("already_running", jobs=0)
    except ServiceError:
        return _service_receipt("error", jobs=0, code="lock_unavailable")

    jobs = 0
    last_code: str | None = None
    try:
        _record_owner(base, pid_lock.pid, pid_lock.nonce, _checked_now(clock))

        cycles = 0
        while True:
            if _event_is_set(stop_event):
                return _service_receipt("stopped", jobs=jobs, code=last_code)
            if checked_cycles is not None and cycles >= checked_cycles:
                return _service_receipt("cycle_limit", jobs=jobs, code=last_code)
            cycles += 1

            now = _checked_now(clock)
            gate = _refresh_queue_gates(base, data_dir, config_loader)
            if not gate["active"]:
                return _service_receipt("paused", jobs=jobs, code=gate["reason"])
            choice = _claim_due_project(base, now, checked_timeout)
            if choice["kind"] == "stop":
                _consume_stop_request(base)
                return _service_receipt("stopped", jobs=jobs, code=last_code)
            if choice["kind"] == "wait":
                _bounded_sleep(sleeper, _sleep_delay(choice.get("due_at"), now, checked_poll))
                continue

            project = choice["project"]
            generation = choice["generation"]
            assert isinstance(project, str)
            assert isinstance(generation, int)
            eligible, reason, processor_enabled, semantic_enabled = _eligibility(
                project, data_dir, config_loader
            )
            if not eligible:
                _park_unprocessed_project(base, project, generation, reason, _checked_now(clock))
                if reason in {"disabled", "service_disabled", "configuration_unavailable"}:
                    return _service_receipt("paused", jobs=jobs, code=reason)
                # One excluded project must not starve a different approved
                # project.  It is parked until configuration changes or a new
                # explicit enqueue refreshes it.
                continue

            retry_failed = bool(choice["retry_requested"])
            if processor_enabled:
                result = _call_processor(
                    active_processor, project, data_dir, retry_failed, checked_timeout
                )
                jobs += 1
                status, code = _processor_outcome(result)
                last_code = code or last_code
            else:
                # Disabling the native processor does not prevent local
                # semantic indexing.  Keep the raw-observation queue intact
                # so re-enabling processing never silently loses its work.
                status, code = "idle", None

            # Indexing also runs when observation processing is idle: explicit
            # memory notes can need embedding even when no raw observations do.
            try:
                index_pending = (
                    _run_indexer(indexer, project, data_dir, retry_failed) if semantic_enabled else False
                )
            except Exception:
                status, code = "failed", "index_failure"
                last_code = code

            if status == "failed":
                # A persisted content rejection is isolated to its leased
                # batch. Store excludes failed sources from ordinary claims;
                # continue new work with retry_failed=False, never rearm it.
                if code == "invalid_response" and _quarantine_rejection(
                    base, project, generation, result.get("job_id"), _checked_now(clock),
                    reason_code=result.get("reason_code"),
                ):
                    continue
                retrying = _finish_failure(
                    base,
                    project,
                    generation,
                    code or "invalid_result",
                    now=_checked_now(clock),
                    max_timeout_retries=checked_retries,
                    retry_backoff=checked_backoff,
                    rejected_job_id=result.get("job_id") if processor_enabled else None,
                    reason_code=result.get("reason_code") if processor_enabled else None,
                )
                if retrying:
                    continue
                # A bad project is quarantined by _finish_failure, but it
                # must not stop unrelated approved work from draining.  Keep
                # the historical halted receipt when this was the only
                # eligible project, so callers still get a clear failure.
                if _has_other_eligible_project(
                    base, data_dir, config_loader, excluded_project=project
                ):
                    continue
                return _service_receipt("halted", jobs=jobs, code=code or "invalid_result")

            if not processor_enabled:
                _finish_local_only(
                    base,
                    project,
                    generation,
                    index_pending,
                    semantic_enabled,
                    _checked_now(clock),
                )
                # Continue across all queued projects.  A drained local index
                # pass parks only this project until processor configuration
                # changes; it cannot repeatedly spin on the same record.
                continue

            _finish_success(
                base,
                project,
                generation,
                status,
                index_pending,
                _checked_now(clock),
            )
    except ServiceStateError:
        return _service_receipt("error", jobs=jobs, code="state_unavailable")
    except ServiceError:
        return _service_receipt("error", jobs=jobs, code="service_unavailable")
    finally:
        try:
            _clear_owner(base, pid_lock.pid, pid_lock.nonce)
        except ServiceError:
            pass
        pid_lock.release()


def start_service(
    data_dir: str | os.PathLike[str] | None = None,
    *,
    launcher: Callable[[list[str], Mapping[str, str]], Any] | None = None,
    clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], Any] = time.sleep,
    startup_timeout: int | float = DEFAULT_STARTUP_TIMEOUT,
) -> dict[str, Any]:
    """Start one detached packaged ``service run`` child.

    This function does not enqueue work and never changes host startup/login
    configuration.  It returns bounded lifecycle states, or ``unknown`` when
    PID-lock visibility is unavailable and starting another worker is unsafe.
    """

    checked_timeout = _validate_nonnegative_number(startup_timeout, "startup_timeout")
    base = _base_dir(data_dir)
    status = service_status(base, clock=clock)
    if status["status"] == "running":
        return {"status": "running", "pid": status.get("pid")}
    if status["status"] == "starting":
        return {"status": "already_starting", "pid": status.get("pid")}
    if status["status"] in {"unknown", "unavailable"}:
        return {
            "status": status["status"],
            "code": status.get("code", "status_unavailable"),
        }
    lock_held = _pid_lock_held(base)
    if lock_held is None:
        return {"status": "unknown", "code": "lock_visibility_unavailable"}
    if lock_held:
        return {"status": "already_starting"}

    try:
        _clear_stop_request(base)
        reservation = _reserve_startup(base, _checked_now(clock))
    except ServiceAlreadyRunning:
        return {"status": "already_starting"}
    except ServiceError:
        return {"status": "error", "code": "startup_unavailable"}

    launcher_script = Path(__file__).resolve().parents[1] / "scripts" / "codex-mem.py"
    command = [
        sys.executable,
        str(launcher_script),
        "--data-dir",
        str(base),
        "service",
        "run",
    ]
    environment = dict(os.environ)
    environment[SERVICE_STARTUP_ENV] = reservation["nonce"]
    try:
        child = (launcher or _spawn_service_child)(command, environment)
        child_pid = _child_pid(child)
        _set_startup_child(base, reservation["nonce"], child_pid)
    except Exception:
        _clear_startup_reservation(base, reservation["nonce"])
        return {"status": "error", "code": "startup_failed"}

    deadline = _checked_now(clock) + checked_timeout
    while _checked_now(clock) < deadline:
        status = service_status(base, clock=clock)
        if status["status"] == "running" and status.get("pid") == child_pid:
            _clear_startup_reservation(base, reservation["nonce"])
            return {"status": "started", "pid": child_pid}
        if _child_exited(child):
            _clear_startup_reservation(base, reservation["nonce"])
            return {"status": "error", "code": "startup_failed"}
        _bounded_sleep(sleeper, min(0.05, max(0.0, deadline - _checked_now(clock))))
    return {"status": "starting", "pid": child_pid}


def stop_service(
    data_dir: str | os.PathLike[str] | None = None,
    *,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Request a bounded cooperative shutdown without signalling a PID."""

    base = _base_dir(data_dir)
    status = service_status(base, clock=clock)
    if status["status"] in {"unknown", "unavailable"}:
        return {
            "status": status["status"],
            "code": status.get("code", "status_unavailable"),
        }
    if status["status"] not in {"running", "starting"}:
        return {"status": "not_running"}
    try:
        with _state_lock(base):
            state = _load_state(base)
            state["stop_requested"] = True
            state["stop_requested_at"] = _checked_now(clock)
            _write_state(base, state)
    except ServiceError:
        return {"status": "error", "code": "state_unavailable"}
    return {"status": "stopping", "pid": status.get("pid")}


def service_status(
    data_dir: str | os.PathLike[str] | None = None,
    *,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Return a safe queue and ownership summary without reading observations."""

    try:
        now = _checked_now(clock)
        base = _base_dir(data_dir)
        state = _load_state_readonly(base)
    except ServiceError:
        return {
            "status": "unavailable",
            "running": None,
            "queued_projects": 0,
            "blocked_projects": 0,
            "code": "status_unavailable",
        }

    lock = _read_pid_lock(base)
    owner = state["owner"]
    lock_held = _pid_lock_held(base)
    projects = state["projects"]
    blocked = sum(1 for record in projects.values() if record["blocked"])
    rejected_projects = [project for project, record in projects.items() if record["rejected_batches"]]
    rejection_counts = _rejection_counts(base, rejected_projects)
    quarantines = {
        project: {"batches": None if rejection_counts is None else rejection_counts.get(project, 0),
                  "last_job_id": projects[project]["last_rejected_job"], "code": "invalid_response",
                  "reason_code": projects[project]["last_rejected_reason"]}
        for project in rejected_projects
        if rejection_counts is None or rejection_counts.get(project, 0)
    }
    quarantine_status = {
        "quarantined_projects": None if rejection_counts is None else len(quarantines),
        "quarantined_batches": None if rejection_counts is None else sum(rejection_counts.values()),
        "quarantines": quarantines,
    }
    if lock_held is None:
        result: dict[str, Any] = {
            "status": "unknown",
            "running": None,
            "queued_projects": len(projects),
            "blocked_projects": blocked,
            "stop_requested": state["stop_requested"],
            "code": "lock_visibility_unavailable",
            **quarantine_status,
        }
        if lock and owner and lock["pid"] == owner["pid"] and lock["nonce"] == owner["nonce"]:
            result["pid"] = lock["pid"]
        return result
    running = bool(
        lock
        and owner
        and lock_held
        and lock["pid"] == owner["pid"]
        and lock["nonce"] == owner["nonce"]
        and _pid_alive(lock["pid"])
    )
    startup = _read_startup_reservation(base)
    starting = bool(
        not running
        and (
            lock_held
            or (
                startup
                and now - startup["started_at"] <= DEFAULT_STARTUP_TTL
                and _pid_alive(startup["pid"])
            )
        )
    )
    result: dict[str, Any] = {
        "status": "running" if running else "starting" if starting else "stopped",
        "running": running,
        "queued_projects": len(projects),
        "blocked_projects": blocked,
        "stop_requested": state["stop_requested"],
        **quarantine_status,
    }
    if running and lock is not None:
        result["pid"] = lock["pid"]
    elif starting and startup is not None:
        result["pid"] = startup["pid"]
    return result


def _base_dir(data_dir: str | os.PathLike[str] | None) -> Path:
    try:
        return data_dir_path(data_dir)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ServiceError("service data directory is unavailable") from None


def _eligibility(
    project: str,
    data_dir: str | os.PathLike[str] | None,
    config_loader: Callable[[str | os.PathLike[str] | None], Mapping[str, Any]],
) -> tuple[bool, str, bool, bool]:
    if hooks_disabled():
        return False, "disabled", False, False
    try:
        config = config_loader(data_dir)
    except Exception:
        return False, "configuration_unavailable", False, False
    if not isinstance(config, Mapping) or not getattr(config, "valid", True):
        return False, "configuration_unavailable", False, False
    service_enabled = config.get("service_enabled", True)
    processor_enabled = config.get("processor_enabled", True)
    semantic_enabled = config.get("semantic_enabled", True)
    if not all(isinstance(value, bool) for value in (service_enabled, processor_enabled, semantic_enabled)):
        return False, "configuration_unavailable", False, False
    if not service_enabled:
        return False, "service_disabled", False, False
    try:
        if not automatic_capture_enabled(project, config):
            return False, "not_selected", False, False
    except Exception:
        return False, "configuration_unavailable", False, False
    return True, "", processor_enabled, semantic_enabled


def _refresh_queue_gates(
    base: Path,
    data_dir: str | os.PathLike[str] | None,
    config_loader: Callable[[str | os.PathLike[str] | None], Mapping[str, Any]],
) -> dict[str, Any]:
    """Refresh parked projects without discovering any new project.

    Scope choices are per project, so a newly excluded record must not pause a
    second selected record.  The queue is capped and contains only explicitly
    enqueued paths, making this bounded local refresh safe.
    """

    if hooks_disabled():
        return {"active": False, "reason": "disabled"}
    try:
        config = config_loader(data_dir)
    except Exception:
        return {"active": False, "reason": "configuration_unavailable"}
    if not isinstance(config, Mapping) or not getattr(config, "valid", True):
        return {"active": False, "reason": "configuration_unavailable"}
    service_enabled = config.get("service_enabled", True)
    processor_enabled = config.get("processor_enabled", True)
    semantic_enabled = config.get("semantic_enabled", True)
    capture_enabled = config.get("capture_enabled", True)
    if not all(
        isinstance(value, bool)
        for value in (service_enabled, processor_enabled, semantic_enabled, capture_enabled)
    ):
        return {"active": False, "reason": "configuration_unavailable"}
    if not service_enabled:
        return {"active": False, "reason": "service_disabled"}
    if not capture_enabled:
        return {"active": False, "reason": "disabled"}

    with _state_lock(base):
        state = _load_state(base)
        changed = False
        for project, record in state["projects"].items():
            try:
                selected = automatic_capture_enabled(project, config)
            except Exception:
                selected = False
            parked = record["parked"]
            if not selected:
                if parked != "not_selected":
                    record["parked"] = "not_selected"
                    changed = True
                continue
            if parked == "not_selected":
                record["parked"] = None
                changed = True
            elif processor_enabled and parked in {"processor_disabled", "semantic_disabled"}:
                record["parked"] = None
                changed = True
            elif not processor_enabled and semantic_enabled and parked == "semantic_disabled":
                record["parked"] = None
                changed = True
        if changed:
            _write_state(base, state)
    return {
        "active": True,
        "reason": "",
        "processor_enabled": processor_enabled,
        "semantic_enabled": semantic_enabled,
    }


def _new_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "cursor": None,
        "stop_requested": False,
        "stop_requested_at": None,
        "owner": None,
        "projects": {},
    }


def _new_record(now: float, *, retry_requested: bool = False) -> dict[str, Any]:
    return {
        "generation": 1,
        "due_at": now,
        "attempts": 0,
        "retry_requested": retry_requested,
        "last_code": None,
        "rejected_batches": 0,
        "last_rejected_job": None,
        "last_rejected_reason": None,
        "blocked": False,
        "parked": None,
        "inflight_generation": None,
        "inflight_until": None,
    }


class _StateLock:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.handle: Any | None = None

    def __enter__(self) -> "_StateLock":
        _ensure_base_dir(self.base)
        target = self.base / SERVICE_STATE_LOCK_FILENAME
        _reject_link_or_nonfile(target)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags, 0o600)
            os.chmod(target, 0o600)
            self.handle = os.fdopen(descriptor, "a+", encoding="utf-8")
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
            return self
        except OSError:
            if self.handle is not None:
                self.handle.close()
            raise ServiceStateError("service state is unavailable") from None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _state_lock(base: Path) -> _StateLock:
    return _StateLock(base)


def _ensure_base_dir(base: Path) -> None:
    try:
        if base.exists() and base.is_symlink():
            raise ServiceStateError("service state is unavailable")
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(base, 0o700)
    except ServiceError:
        raise
    except OSError:
        raise ServiceStateError("service state is unavailable") from None


def _reject_link_or_nonfile(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError:
        raise ServiceStateError("service state is unavailable") from None
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ServiceStateError("service state is unavailable")


def _load_state_readonly(base: Path) -> dict[str, Any]:
    destination = base / SERVICE_STATE_FILENAME
    if not destination.exists():
        return _new_state()
    return _read_json_state(destination)


def _load_state(base: Path) -> dict[str, Any]:
    return _load_state_readonly(base)


def _read_json_state(destination: Path) -> dict[str, Any]:
    _reject_link_or_nonfile(destination)
    try:
        if destination.stat().st_size > MAX_STATE_BYTES:
            raise ServiceStateError("service state is unavailable")
        with destination.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except ServiceError:
        raise
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        raise ServiceStateError("service state is unavailable") from None
    return _validate_state(raw)


def _validate_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or raw.get("version") != STATE_VERSION:
        raise ServiceStateError("service state is unavailable")
    projects = raw.get("projects")
    if not isinstance(projects, Mapping) or len(projects) > MAX_QUEUED_PROJECTS:
        raise ServiceStateError("service state is unavailable")
    normalised_projects: dict[str, dict[str, Any]] = {}
    for project, record in projects.items():
        try:
            valid_project = isinstance(project, str) and project_key(project) == project
        except (OSError, RuntimeError, TypeError, ValueError):
            valid_project = False
        if not valid_project:
            raise ServiceStateError("service state is unavailable")
        normalised_projects[project] = _validate_record(record)
    cursor = raw.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or cursor not in normalised_projects):
        cursor = None
    stop_requested = raw.get("stop_requested")
    if not isinstance(stop_requested, bool):
        raise ServiceStateError("service state is unavailable")
    stop_requested_at = raw.get("stop_requested_at")
    if stop_requested_at is not None and not _is_timestamp(stop_requested_at):
        raise ServiceStateError("service state is unavailable")
    owner = _validate_owner(raw.get("owner"))
    return {
        "version": STATE_VERSION,
        "cursor": cursor,
        "stop_requested": stop_requested,
        "stop_requested_at": stop_requested_at,
        "owner": owner,
        "projects": normalised_projects,
    }


def _validate_record(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ServiceStateError("service state is unavailable")
    generation = raw.get("generation")
    attempts = raw.get("attempts")
    retry_requested = raw.get("retry_requested")
    blocked = raw.get("blocked")
    parked = raw.get("parked")
    due_at = raw.get("due_at")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or not 1 <= generation <= 1_000_000_000
        or isinstance(attempts, bool)
        or not isinstance(attempts, int)
        or not 0 <= attempts <= 1_000_000_000
        or not isinstance(retry_requested, bool)
        or not isinstance(blocked, bool)
        or (parked is not None and (not isinstance(parked, str) or parked not in _PARK_REASONS))
        or not _is_timestamp(due_at)
    ):
        raise ServiceStateError("service state is unavailable")
    last_code = raw.get("last_code")
    if last_code is not None and not _safe_code(last_code):
        raise ServiceStateError("service state is unavailable")
    rejected_batches = raw.get("rejected_batches", 0)
    last_rejected_job = raw.get("last_rejected_job")
    last_rejected_reason = raw.get("last_rejected_reason")
    if (isinstance(rejected_batches, bool) or not isinstance(rejected_batches, int)
            or not 0 <= rejected_batches <= 1_000_000_000
            or (rejected_batches == 0) != (last_rejected_job is None)
            or (last_rejected_job is not None and not _valid_job_id(last_rejected_job))):
        raise ServiceStateError("service state is unavailable")
    if last_rejected_reason is not None and (
        not rejected_batches or not isinstance(last_rejected_reason, str)
        or last_rejected_reason not in INVALID_RESPONSE_REASONS
    ):
        raise ServiceStateError("service state is unavailable")
    inflight_generation = raw.get("inflight_generation")
    inflight_until = raw.get("inflight_until")
    if inflight_generation is not None and (
        isinstance(inflight_generation, bool)
        or not isinstance(inflight_generation, int)
        or not 1 <= inflight_generation <= generation
    ):
        raise ServiceStateError("service state is unavailable")
    if inflight_until is not None and not _is_timestamp(inflight_until):
        raise ServiceStateError("service state is unavailable")
    if (inflight_generation is None) != (inflight_until is None):
        raise ServiceStateError("service state is unavailable")
    return {
        "generation": generation,
        "due_at": float(due_at),
        "attempts": attempts,
        "retry_requested": retry_requested,
        "last_code": last_code,
        "rejected_batches": rejected_batches,
        "last_rejected_job": last_rejected_job,
        "last_rejected_reason": last_rejected_reason,
        "blocked": blocked,
        "parked": parked,
        "inflight_generation": inflight_generation,
        "inflight_until": None if inflight_until is None else float(inflight_until),
    }


def _validate_owner(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ServiceStateError("service state is unavailable")
    pid = raw.get("pid")
    nonce = raw.get("nonce")
    started_at = raw.get("started_at")
    if not _valid_pid(pid) or not _valid_nonce(nonce) or not _is_timestamp(started_at):
        raise ServiceStateError("service state is unavailable")
    return {"pid": pid, "nonce": nonce, "started_at": float(started_at)}


def _write_state(base: Path, state: Mapping[str, Any]) -> None:
    _validate_state(state)
    _atomic_write_json(base / SERVICE_STATE_FILENAME, state)


def _atomic_write_json(destination: Path, value: Mapping[str, Any]) -> None:
    _reject_link_or_nonfile(destination)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(value, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, destination)
    except ServiceError:
        raise
    except OSError:
        raise ServiceStateError("service state is unavailable") from None
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


class _PidLock:
    def __init__(self, base: Path, started_at: float) -> None:
        self.base = base
        self.started_at = started_at
        self.pid = os.getpid()
        self.nonce = secrets.token_hex(16)
        self.acquired = False
        self.handle: Any | None = None

    @property
    def path(self) -> Path:
        return self.base / SERVICE_PID_FILENAME

    def acquire(self) -> None:
        _ensure_base_dir(self.base)
        if fcntl is None:  # A missing cross-process lock must fail closed.
            raise ServiceError("service lock is unavailable")
        _reject_link_or_nonfile(self.path)
        try:
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags, 0o600)
            os.chmod(self.path, 0o600)
            handle = os.fdopen(descriptor, "r+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                raise ServiceAlreadyRunning("service already running") from None
            payload = {
                "version": STATE_VERSION,
                "pid": self.pid,
                "nonce": self.nonce,
                "started_at": self.started_at,
            }
            handle.seek(0)
            handle.truncate()
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
            self.handle = handle
            self.acquired = True
        except ServiceAlreadyRunning:
            raise
        except (OSError, TypeError, ValueError):
            raise ServiceError("service lock is unavailable") from None

    def release(self) -> None:
        if not self.acquired or self.handle is None:
            return
        try:
            # Keep the inode stable: unlinking a locked file would let another
            # process create a new path and bypass the held flock.  Empty
            # metadata is harmless because status also checks lock ownership.
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.flush()
            os.fsync(self.handle.fileno())
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None
            self.acquired = False


def _read_pid_lock(base: Path) -> dict[str, Any] | None:
    return _read_lifecycle_file(base / SERVICE_PID_FILENAME)


def _pid_lock_held(base: Path) -> bool | None:
    """Return PID-lock ownership, or ``None`` if read-only inspection is denied."""

    if fcntl is None:
        return None
    path = base / SERVICE_PID_FILENAME
    try:
        _reject_link_or_nonfile(path)
        # Status does not modify the lock; opening read-only avoids requiring
        # write permission merely to observe a worker owned by another host
        # context or sandbox.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        return False
    except (PermissionError, ServiceError):
        return None
    except OSError:
        return None
    return False


def _read_startup_reservation(base: Path) -> dict[str, Any] | None:
    return _read_lifecycle_file(base / SERVICE_STARTUP_FILENAME)


def _read_lifecycle_file(path: Path) -> dict[str, Any] | None:
    try:
        _reject_link_or_nonfile(path)
        if not path.exists() or path.stat().st_size > 4_096:
            return None
        with path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError, ServiceError):
        return None
    if not isinstance(raw, Mapping):
        return None
    pid = raw.get("pid")
    nonce = raw.get("nonce")
    started_at = raw.get("started_at")
    if not _valid_pid(pid) or not _valid_nonce(nonce) or not _is_timestamp(started_at):
        return None
    return {"pid": pid, "nonce": nonce, "started_at": float(started_at)}


def _record_owner(base: Path, pid: int, nonce: str, started_at: float) -> None:
    with _state_lock(base):
        state = _load_state(base)
        state["owner"] = {"pid": pid, "nonce": nonce, "started_at": started_at}
        _write_state(base, state)


def _clear_owner(base: Path, pid: int, nonce: str) -> None:
    with _state_lock(base):
        state = _load_state(base)
        owner = state["owner"]
        if owner and owner["pid"] == pid and owner["nonce"] == nonce:
            state["owner"] = None
            _write_state(base, state)


def _reserve_startup(base: Path, now: float) -> dict[str, Any]:
    _ensure_base_dir(base)
    path = base / SERVICE_STARTUP_FILENAME
    existing = _read_startup_reservation(base)
    if existing is not None:
        if now - existing["started_at"] <= DEFAULT_STARTUP_TTL and _pid_alive(existing["pid"]):
            raise ServiceAlreadyRunning("service is starting")
        try:
            path.unlink(missing_ok=True)
        except OSError:
            raise ServiceError("service startup is unavailable") from None
    reservation = {
        "version": STATE_VERSION,
        "pid": os.getpid(),
        "nonce": secrets.token_hex(16),
        "started_at": now,
    }
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, json.dumps(reservation, separators=(",", ":")).encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(path, 0o600)
    except FileExistsError:
        raise ServiceAlreadyRunning("service is starting") from None
    except OSError:
        raise ServiceError("service startup is unavailable") from None
    return reservation


def _set_startup_child(base: Path, nonce: str, child_pid: int) -> None:
    path = base / SERVICE_STARTUP_FILENAME
    current = _read_startup_reservation(base)
    if current is None or current["nonce"] != nonce:
        raise ServiceError("service startup is unavailable")
    payload = {"version": STATE_VERSION, "pid": child_pid, "nonce": nonce, "started_at": current["started_at"]}
    _atomic_write_json(path, payload)


def _clear_startup_reservation(base: Path, nonce: str) -> None:
    path = base / SERVICE_STARTUP_FILENAME
    current = _read_startup_reservation(base)
    if current is None or current["nonce"] != nonce:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _clear_stop_request(base: Path) -> None:
    with _state_lock(base):
        state = _load_state(base)
        if state["stop_requested"]:
            state["stop_requested"] = False
            state["stop_requested_at"] = None
            _write_state(base, state)


def _consume_stop_request(base: Path) -> None:
    with _state_lock(base):
        state = _load_state(base)
        if state["stop_requested"]:
            state["stop_requested"] = False
            state["stop_requested_at"] = None
            _write_state(base, state)


def _claim_due_project(base: Path, now: float, processor_timeout: float) -> dict[str, Any]:
    with _state_lock(base):
        state = _load_state(base)
        if state["stop_requested"]:
            return {"kind": "stop"}
        projects = state["projects"]
        for record in projects.values():
            if record["inflight_until"] is not None and record["inflight_until"] <= now:
                record["inflight_generation"] = None
                record["inflight_until"] = None
                record["due_at"] = min(record["due_at"], now)

        ready = [
            project
            for project, record in projects.items()
            if not record["blocked"]
            and record["parked"] is None
            and record["inflight_generation"] is None
            and record["due_at"] <= now
        ]
        if not ready:
            due_values = [
                record["due_at"]
                for record in projects.values()
                if not record["blocked"]
                and record["parked"] is None
                and record["inflight_generation"] is None
            ]
            _write_state(base, state)
            return {"kind": "wait", "due_at": min(due_values) if due_values else None}

        ordered = sorted(ready)
        cursor = state["cursor"]
        chosen = next((item for item in ordered if cursor is None or item > cursor), ordered[0])
        record = projects[chosen]
        record["inflight_generation"] = record["generation"]
        record["inflight_until"] = now + processor_timeout + _INFLIGHT_GRACE_SECONDS
        state["cursor"] = chosen
        _write_state(base, state)
        return {
            "kind": "work",
            "project": chosen,
            "generation": record["generation"],
            "attempts": record["attempts"],
            "retry_requested": record["retry_requested"],
        }


def _park_unprocessed_project(
    base: Path, project: str, generation: int, reason: str, now: float
) -> None:
    with _state_lock(base):
        state = _load_state(base)
        record = state["projects"].get(project)
        if record is None:
            return
        if record["inflight_generation"] == generation:
            record["inflight_generation"] = None
            record["inflight_until"] = None
            record["due_at"] = min(record["due_at"], now)
            if reason == "not_selected":
                record["parked"] = "not_selected"
            _write_state(base, state)


def _has_other_eligible_project(
    base: Path,
    data_dir: str | os.PathLike[str] | None,
    config_loader: Callable[[str | os.PathLike[str] | None], Mapping[str, Any]],
    *,
    excluded_project: str,
) -> bool:
    """Return whether another queued project can still make progress.

    The state read is deliberately separate from claiming work: a failed
    project is already blocked, and the next loop iteration can then use the
    normal fair claim path for whichever eligible record remains.  Projects
    parked for scope/configuration are not considered, while a due time or
    inflight lease is allowed to keep the worker alive until that work is
    ready.
    """

    with _state_lock(base):
        state = _load_state(base)
        candidates = [
            project
            for project, record in state["projects"].items()
            if project != excluded_project
            and not record["blocked"]
            and record["parked"] is None
        ]
    for project in candidates:
        eligible, _, _, _ = _eligibility(project, data_dir, config_loader)
        if eligible:
            return True
    return False


def _finish_success(
    base: Path,
    project: str,
    generation: int,
    status: str,
    index_pending: bool,
    now: float,
) -> None:
    with _state_lock(base):
        state = _load_state(base)
        record = state["projects"].get(project)
        if record is None:
            return
        record["inflight_generation"] = None
        record["inflight_until"] = None
        record["attempts"] = 0
        record["retry_requested"] = False
        if record["rejected_batches"]:
            counts = _rejection_counts(base, [project])
            if counts is not None and not counts.get(project, 0):
                record["rejected_batches"] = 0
                record["last_rejected_job"] = None
                record["last_rejected_reason"] = None
        record["last_code"] = "invalid_response" if record["rejected_batches"] else None
        if record["generation"] != generation:
            record["due_at"] = now
        elif status == "idle" and not index_pending:
            if record["rejected_batches"]:
                # Preserve a visible warning while dormant. Ordinary enqueue
                # wakes new work; polling cannot repeatedly invoke the model.
                record["parked"] = "rejected_batches"
            else:
                del state["projects"][project]
                if state["cursor"] == project:
                    state["cursor"] = None
        else:
            # A processed/skipped batch can be followed by more raw entries.
            # Keep draining until one invocation reports idle.
            record["due_at"] = now
        _write_state(base, state)


def _finish_local_only(
    base: Path,
    project: str,
    generation: int,
    index_pending: bool,
    semantic_enabled: bool,
    now: float,
) -> None:
    """Release a local-index-only pass without removing raw observations."""

    with _state_lock(base):
        state = _load_state(base)
        record = state["projects"].get(project)
        if record is None:
            return
        record["inflight_generation"] = None
        record["inflight_until"] = None
        # An enqueue during the index pass already advanced generation.  In
        # either case the project remains explicitly queued for the future AI
        # worker; only the local indexing work may be considered drained.
        record["due_at"] = now
        if record["generation"] != generation:
            record["parked"] = None
        elif index_pending:
            record["parked"] = None
        elif semantic_enabled:
            record["parked"] = "processor_disabled"
        else:
            record["parked"] = "semantic_disabled"
        _write_state(base, state)


def _valid_job_id(value: Any) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 64
            and all(c in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in value))


def _persisted_rejection(base: Path, project: str, job_id: Any) -> bool:
    """Confirm a durable failed batch before permitting independent work.

    Read job metadata only through a read-only SQLite connection. A missing,
    malformed, foreign-project, or uncommitted receipt remains fail-closed.
    """

    if not _valid_job_id(job_id):
        return False
    connection = None
    try:
        database = base / "memory.sqlite3"
        _reject_link_or_nonfile(database)
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2.0)
        row = connection.execute(
            "SELECT j.id FROM observation_jobs AS j WHERE j.id = ? AND j.project = ? "
            "AND j.processor_id = ? AND j.model = ? AND j.reasoning_effort = ? "
            "AND j.status = 'failed' AND j.error_code = 'invalid_response' "
            "AND j.lease_token IS NULL AND j.lease_expires_at IS NULL "
            "AND EXISTS (SELECT 1 FROM observation_job_sources AS links WHERE links.job_id = j.id) "
            "AND NOT EXISTS (SELECT 1 FROM observation_job_sources AS links "
            "LEFT JOIN entries AS e ON e.id = links.source_id AND e.project = j.project "
            "WHERE links.job_id = j.id AND e.id IS NULL)",
            (job_id, project, PROCESSOR_ID, MODEL, REASONING_EFFORT),
        ).fetchone()
        return row is not None
    except (OSError, sqlite3.Error, ServiceError, ValueError):
        return False
    finally:
        if connection is not None:
            connection.close()


def _rejection_counts(base: Path, projects: list[str]) -> dict[str, int] | None:
    """Read unresolved failures, so a later explicit repair clears warnings."""

    if not projects:
        return {}
    connection = None
    try:
        database = base / "memory.sqlite3"
        _reject_link_or_nonfile(database)
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2.0)
        placeholders = ",".join("?" for _ in projects)
        rows = connection.execute(
            "SELECT project, COUNT(*) FROM observation_jobs "
            f"WHERE project IN ({placeholders}) AND processor_id = ? AND model = ? "
            "AND reasoning_effort = ? AND status = 'failed' AND error_code = 'invalid_response' "
            "GROUP BY project", (*projects, PROCESSOR_ID, MODEL, REASONING_EFFORT),
        ).fetchall()
        return {project: count for project, count in rows}
    except (OSError, sqlite3.Error, ServiceError, ValueError):
        return None
    finally:
        if connection is not None:
            connection.close()


def _record_rejection(record: dict[str, Any], job_id: str, reason_code: Any = None) -> None:
    if record["last_rejected_job"] != job_id:
        record["rejected_batches"] += 1
        record["last_rejected_reason"] = None
    record["last_rejected_job"] = job_id
    if isinstance(reason_code, str) and reason_code in INVALID_RESPONSE_REASONS:
        record["last_rejected_reason"] = reason_code
    record["last_code"] = "invalid_response"


def _quarantine_rejection(
    base: Path, project: str, generation: int, job_id: Any, now: float, *, reason_code: Any = None,
) -> bool:
    # Unknown historic reasons require explicit resume_pending after review.
    # Runner envelopes, lifecycle, and internal source corruption may signal
    # infrastructure failures and cannot be treated as bad model content.
    if not isinstance(reason_code, str) or reason_code not in _CONTENT_REJECTION_REASONS:
        return False
    if not _persisted_rejection(base, project, job_id):
        return False
    with _state_lock(base):
        state = _load_state(base)
        record = state["projects"].get(project)
        if (record is None or record["inflight_generation"] != generation
                or record["last_rejected_job"] == job_id):
            # Repeated identical receipts are not progress. Do not spin on a
            # broken adapter that keeps returning a previously rejected job.
            return False
        _record_rejection(record, job_id, reason_code)
        record["inflight_generation"] = None
        record["inflight_until"] = None
        record["retry_requested"] = False
        record["attempts"] = 0
        record["blocked"] = False
        record["due_at"] = now
        _write_state(base, state)
    return True


def _finish_failure(
    base: Path,
    project: str,
    generation: int,
    code: str,
    *,
    now: float,
    max_timeout_retries: int,
    retry_backoff: float,
    rejected_job_id: Any = None,
    reason_code: Any = None,
) -> bool:
    safe_code = _normalise_code(code)
    rejected = safe_code == "invalid_response" and _persisted_rejection(base, project, rejected_job_id)
    with _state_lock(base):
        state = _load_state(base)
        record = state["projects"].get(project)
        if record is None:
            return False
        record["inflight_generation"] = None
        record["inflight_until"] = None
        if rejected:
            _record_rejection(record, rejected_job_id, reason_code)
        record["last_code"] = safe_code
        if safe_code == "timeout":
            record["attempts"] += 1
            if record["attempts"] <= max_timeout_retries:
                record["retry_requested"] = True
                delay = min(MAX_BACKOFF_SECONDS, retry_backoff * (2 ** (record["attempts"] - 1)))
                record["due_at"] = now + delay
                _write_state(base, state)
                return True
        record["blocked"] = True
        record["due_at"] = now
        _write_state(base, state)
        return False


def _choose_processor(
    processor: Callable[..., Mapping[str, Any]] | None,
    runner: Callable[..., Mapping[str, Any]] | None,
) -> Callable[..., Mapping[str, Any]]:
    if processor is not None and runner is not None:
        raise ValueError("pass only one processor or runner")
    active = processor if processor is not None else runner
    if active is None:
        return process_pending
    if not callable(active):
        raise ValueError("processor must be callable")
    return active


def _call_processor(
    processor: Callable[..., Mapping[str, Any]],
    project: str,
    data_dir: str | os.PathLike[str] | None,
    retry_failed: bool,
    timeout: float,
) -> Mapping[str, Any]:
    try:
        value = processor(project, data_dir=data_dir, retry_failed=retry_failed, timeout=timeout)
    except Exception:
        return {"status": "failed", "code": "runner_failure"}
    return value if isinstance(value, Mapping) else {"status": "failed", "code": "invalid_result"}


def _processor_outcome(value: Mapping[str, Any]) -> tuple[str, str | None]:
    status = value.get("status")
    if status in {"processed", "skipped", "idle"}:
        return str(status), None
    if status == "failed":
        code = value.get("code")
        return "failed", _normalise_code(code) if isinstance(code, str) else "invalid_result"
    return "failed", "invalid_result"


def _run_indexer(
    indexer: Callable[..., Any] | None,
    project: str,
    data_dir: str | os.PathLike[str] | None,
    retry_failed: bool,
) -> bool:
    if indexer is None:
        return False
    if retry_failed:
        return _index_pending(indexer(project, data_dir, retry_failed=True))
    return _index_pending(indexer(project, data_dir))


def _index_pending(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value > 0
    if isinstance(value, Mapping):
        status = value.get("status")
        if status in {"failed", "error"}:
            # The semantic backend already exposes its detailed code locally;
            # queue state needs only one fixed, non-sensitive failure class.
            raise ServiceError("index_failure")
        if "pending" in value:
            return _index_pending(value["pending"])
        if "remaining" in value:
            return _index_pending(value["remaining"])
    raise ServiceError("indexer returned an invalid result")


def _spawn_service_child(command: list[str], environment: Mapping[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        env=dict(environment),
    )


def _child_pid(child: Any) -> int:
    pid = getattr(child, "pid", None)
    if not _valid_pid(pid):
        raise ServiceError("service startup is unavailable")
    return pid


def _child_exited(child: Any) -> bool:
    poll = getattr(child, "poll", None)
    if not callable(poll):
        return False
    try:
        return poll() is not None
    except Exception:
        return True


def _service_receipt(status: str, *, jobs: int, code: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "jobs": jobs}
    if code:
        result["code"] = _normalise_code(code)
    return result


def _valid_pid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 2_147_483_647


def _valid_nonce(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 32 and all(char in "0123456789abcdef" for char in value)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _safe_code(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 64 and all(char in _SAFE_CODE_CHARS for char in value)


def _normalise_code(value: str) -> str:
    if _safe_code(value):
        return value
    return "invalid_result"


def _is_timestamp(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and float(value) >= 0


def _checked_now(clock: Callable[[], float]) -> float:
    try:
        value = clock()
    except Exception:
        raise ServiceError("service clock is unavailable") from None
    if not _is_timestamp(value):
        raise ServiceError("service clock is unavailable")
    return float(value)


def _validate_timeout(value: Any) -> float:
    checked = _validate_positive_number(value, "processor_timeout")
    if checked > 600:
        raise ValueError("processor_timeout must be between 1 and 600")
    return checked


def _validate_positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0:
        raise ValueError(f"{field} must be positive")
    return float(value)


def _validate_nonnegative_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
        raise ValueError(f"{field} must be non-negative")
    return float(value)


def _validate_retries(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TIMEOUT_RETRIES:
        raise ValueError(f"max_timeout_retries must be between 0 and {MAX_TIMEOUT_RETRIES}")
    return value


def _validate_cycles(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("max_cycles must be a non-negative integer")
    return value


def _validate_stop_event(value: Any) -> None:
    if value is not None and not callable(getattr(value, "is_set", None)):
        raise ValueError("stop_event must provide is_set")


def _event_is_set(event: Any | None) -> bool:
    return bool(event is not None and event.is_set())


def _sleep_delay(due_at: Any, now: float, poll_interval: float) -> float:
    if due_at is None:
        return poll_interval
    if not _is_timestamp(due_at):
        return poll_interval
    return min(poll_interval, max(0.0, float(due_at) - now))


def _bounded_sleep(sleeper: Callable[[float], Any], delay: float) -> None:
    # A zero delay is useful for deterministic tests and never blocks a normal
    # service because the default polling interval is one second.
    try:
        sleeper(delay)
    except Exception:
        raise ServiceError("service wait is unavailable") from None
