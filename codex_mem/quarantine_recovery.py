"""Durable, one-use recovery permissions dispatched by the service coordinator.

Only metadata is stored here. Scheduling never claims observations or calls a
model. A permission is consumed before dispatch, so a crash cannot replay it.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Callable

from .config import automatic_capture_enabled, data_dir_path, load_config
from .processor import MODEL, PROCESSOR_ID, REASONING_EFFORT, process_pending, _valid_source_id
from .store import project_key
from .observation_diagnostics import _FAILURE_CODES, safe_failure_reason

STATE_FILENAME = "quarantine-recovery.json"
LOCK_FILENAME = ".quarantine-recovery.lock"
MAX_PERMISSIONS = 500
MAX_STATE_BYTES = 1024 * 1024
ERROR_CODES = frozenset({"invalid_response", "timeout", "runner_failure", "storage_failure"})


class RecoveryStateError(RuntimeError):
    pass


def _check_file(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RecoveryStateError("recovery state unavailable")


@contextmanager
def _locked(base: Path):
    if base.is_symlink():
        raise RecoveryStateError("recovery directory unavailable")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = base / LOCK_FILENAME
    _check_file(target)
    fd = os.open(target, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+") as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load(base: Path) -> dict[str, Any]:
    target = base / STATE_FILENAME
    _check_file(target)
    if not target.exists():
        return {"version": 1, "permissions": []}
    if target.stat().st_size > MAX_STATE_BYTES:
        raise RecoveryStateError("recovery state unavailable")
    try:
        value = json.loads(target.read_text())
        if (not isinstance(value, dict) or set(value) != {"version", "permissions"}
                or value["version"] != 1 or not isinstance(value["permissions"], list)
                or len(value["permissions"]) > MAX_PERMISSIONS):
            raise ValueError
        keys = set()
        for item in value["permissions"]:
            if not isinstance(item, dict) or set(item) != {"id", "project", "job_id", "error_code",
                    "attempt_count", "input_fingerprint", "allow_previous_profile", "state", "outcome"}:
                raise ValueError
            _validate(item["job_id"], item["error_code"], item["attempt_count"], item["input_fingerprint"])
            if (not isinstance(item["project"], str) or project_key(item["project"]) != item["project"]
                    or not isinstance(item["allow_previous_profile"], bool)
                    or item["state"] not in {"scheduled", "dispatching", "complete"}
                    or item["id"] != _identity(item) or item["id"] in keys
                    or item["outcome"] not in {None, "processed", "skipped", "failed", "unavailable", "disabled"}):
                raise ValueError
            keys.add(item["id"])
        return value
    except (ValueError, TypeError, KeyError, OSError):
        raise RecoveryStateError("recovery state unavailable") from None


def _save(base: Path, value: dict[str, Any]) -> None:
    target = base / STATE_FILENAME
    _check_file(target)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=base, prefix=".recovery-", delete=False) as handle:
            name = handle.name
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, target)
        name = None
        directory = os.open(base, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if name is not None:
            os.unlink(name)


def _validate(job_id, error_code, attempt_count, fingerprint):
    if (not isinstance(job_id, str) or not _valid_source_id(job_id) or error_code not in ERROR_CODES
            or isinstance(attempt_count, bool) or not isinstance(attempt_count, int) or attempt_count < 1
            or not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)):
        raise ValueError("recovery requires an exact failed snapshot")


def _identity(item):
    values = [item[k] for k in ("project", "job_id", "error_code", "attempt_count", "input_fingerprint", "allow_previous_profile")]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def _eligibility(project, base):
    config = load_config(base)
    if not getattr(config, "valid", True):
        return "configuration_unavailable"
    if not config.get("processor_enabled", True):
        return "processor_disabled"
    if not automatic_capture_enabled(project, config):
        return "not_selected"
    return None


def _snapshot(base, project, job_id):
    """Read metadata only, without opening a writable Store connection."""
    database = base / "memory.sqlite3"
    _check_file(database)
    if not database.exists():
        return None
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT status,error_code,attempt_count,input_fingerprint,processor_id,"
            "model,reasoning_effort,lease_token,lease_expires_at,output_ids_json FROM observation_jobs "
            "WHERE project=? AND id=?", (project, job_id)).fetchone()
        if row is None:
            return None
        snapshot = dict(row)
        relation_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observation_job_recoveries'"
        ).fetchone()
        snapshot["has_successor"] = bool(relation_exists and connection.execute(
            "SELECT 1 FROM observation_job_recoveries WHERE parent_job_id=? AND project=?",
            (job_id, project)).fetchone())
        return snapshot


def _matches(item, snapshot):
    return snapshot is not None and (not snapshot["has_successor"] and snapshot["status"] == "failed"
        and snapshot["error_code"] == item["error_code"] and snapshot["attempt_count"] == item["attempt_count"]
        and snapshot["input_fingerprint"] == item["input_fingerprint"]
        and snapshot["processor_id"] == PROCESSOR_ID
        and (snapshot["model"] == MODEL or (item["allow_previous_profile"] and snapshot["model"] == "gpt-5.6-luna"))
        and snapshot["reasoning_effort"] == REASONING_EFFORT
        and snapshot["lease_token"] is None and snapshot["lease_expires_at"] is None
        and snapshot["output_ids_json"] == "[]")


def schedule(project, data_dir=None, *, job_id, error_code, attempt_count, input_fingerprint, allow_previous_profile=False):
    """Save one immutable permission; identical authorization is idempotent."""
    _validate(job_id, error_code, attempt_count, input_fingerprint)
    if not isinstance(allow_previous_profile, bool):
        raise ValueError("allow_previous_profile must be true or false")
    workspace, base = project_key(project), data_dir_path(data_dir)
    item = dict(project=workspace, job_id=job_id, error_code=error_code, attempt_count=attempt_count,
                input_fingerprint=input_fingerprint, allow_previous_profile=allow_previous_profile, state="scheduled", outcome=None)
    item["id"] = _identity(item)
    with _locked(base):
        state = _load(base)
        previous = next((row for row in state["permissions"] if row["id"] == item["id"]), None)
        if previous:
            return {"status": "already_scheduled", "permission": dict(previous)}
        disabled = _eligibility(workspace, base)
        if disabled:
            return {"status": "disabled", "code": disabled}
        snapshot = _snapshot(base, workspace, job_id)
        if snapshot and ((snapshot["model"] != MODEL and not (allow_previous_profile and snapshot["model"] == "gpt-5.6-luna")) or snapshot["reasoning_effort"] != REASONING_EFFORT
                         or snapshot["processor_id"] != PROCESSOR_ID):
            return {"status": "blocked", "code": "unsupported_profile"}
        if not _matches(item, snapshot):
            return {"status": "blocked", "code": "recovery_unavailable"}
        if len(state["permissions"]) >= MAX_PERMISSIONS:
            return {"status": "blocked", "code": "recovery_queue_full"}
        state["permissions"].append(item)
        _save(base, state)
    return {"status": "queued", "permission": dict(item)}


def status(data_dir=None, *, project=None):
    base = data_dir_path(data_dir)
    with _locked(base):
        items = _load(base)["permissions"]
        workspace = project_key(project) if project is not None else None
        return [dict(row) for row in items if workspace is None or row["project"] == workspace]


def run_next(project, data_dir=None, *, processor: Callable[..., Any] | None = None, **processor_kwargs):
    """Consume one permission before dispatch; call only from owning coordinator.

    The returned envelope is a maintenance receipt, not a normal processor
    outcome. Integration must never feed its nested failure into auto retry.
    Interrupted dispatch remains consumed and requires a new inspection.
    """
    workspace, base = project_key(project), data_dir_path(data_dir)
    forbidden = {"retry_failed", "retry_job_id", "retry_error_code", "retry_attempt_count", "retry_input_fingerprint", "retry_previous_profile", "retry_one_shot"}
    if forbidden.intersection(processor_kwargs):
        raise ValueError("recovery selectors belong to the permission")
    if not (base / STATE_FILENAME).exists() and not (base / STATE_FILENAME).is_symlink():
        return None
    with _locked(base):
        state = _load(base)
        item = next((row for row in state["permissions"] if row["project"] == workspace
                     and row["state"] == "scheduled"), None)
        if item is None:
            return None
        disabled = _eligibility(workspace, base)
        snapshot = _snapshot(base, workspace, item["job_id"])
        matches = _matches(item, snapshot)
        item["state"] = "complete" if disabled or not matches else "dispatching"
        item["outcome"] = "disabled" if disabled else "unavailable" if not matches else None
        _save(base, state)
        permission = dict(item)
    if permission["state"] == "complete":
        return {"permission": permission, "result": {"status": "blocked", "code": disabled or "recovery_unavailable", "recovery_one_shot": True}}
    active_processor = processor or process_pending
    no_claim_receipt = False
    try:
        profile_arguments = ({"retry_previous_profile": True}
                             if permission["allow_previous_profile"] and snapshot["model"] != MODEL else {})
        result = active_processor(workspace, data_dir=base, retry_failed=False,
            retry_job_id=permission["job_id"], retry_error_code=permission["error_code"],
            retry_attempt_count=permission["attempt_count"], retry_input_fingerprint=permission["input_fingerprint"],
            retry_one_shot=True, **profile_arguments, **processor_kwargs)
        no_claim_receipt = isinstance(result, dict) and (
            result.get("status") == "deferred" or
            (result.get("status") == "blocked" and result.get("code") == "recovery_unavailable"))
        outcome = result.get("status") if isinstance(result, dict) else None
        outcome = outcome if outcome in {"processed", "skipped", "failed"} else "unavailable"
        # Keep only fixed receipt fields. Never persist generated model text.
        safe_result = {"status": outcome, "parent_job_id": permission["job_id"], "recovery_one_shot": True}
        code = result.get("code") if isinstance(result, dict) else None
        if isinstance(code, str) and code in _FAILURE_CODES:
            safe_result["code"] = code
            reason = safe_failure_reason(code, result.get("reason_code"))
            if reason is not None:
                safe_result["reason_code"] = reason
        returned_id = result.get("job_id") if isinstance(result, dict) else None
        safe_result["job_id"] = returned_id if isinstance(returned_id, str) and _valid_source_id(returned_id) else permission["job_id"]
    except Exception:
        outcome, safe_result = "unavailable", {"status": "unavailable", "job_id": permission["job_id"], "recovery_one_shot": True}
    with _locked(base):
        state = _load(base)
        current = next(row for row in state["permissions"] if row["id"] == permission["id"])
        # A busy Store lane does not spend authorization. The receipt alone is
        # insufficient: the exact failed attempt must still exist, with no
        # lease, outputs, incremented attempt or legacy successor. A claimed
        # attempt or interrupted dispatch remains consumed even if its adapter
        # reports a misleading blocked/deferred result.
        if no_claim_receipt and _matches(permission, _snapshot(base, workspace, permission["job_id"])):
            current.update(state="scheduled", outcome=None)
            safe_result = {"status": "deferred", "code": "recovery_unavailable",
                           "job_id": permission["job_id"], "recovery_one_shot": True}
        else:
            current.update(state="complete", outcome=outcome)
        _save(base, state)
        permission = dict(current)
    return {"permission": permission, "result": safe_result}
