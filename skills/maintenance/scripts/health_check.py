#!/usr/bin/env python3
"""Read-only Codex Mem maintenance diagnostics.

This module intentionally does not construct :class:`codex_mem.store.Store`.
Store construction prepares paths and may migrate SQLite, which is unsuitable
for a health check.  The helper opens the existing database with ``mode=ro``
and reads only bounded metadata from the database and local state files.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Iterable, Mapping
from urllib.parse import quote

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


REPORT_VERSION = 2
EXPECTED_SCHEMA_VERSION = 4
EXPECTED_PROCESSOR_MODEL = "gpt-5.6-luna"
EXPECTED_PROCESSOR_EFFORT = "medium"
EXPECTED_SEMANTIC_MODEL = "intfloat/multilingual-e5-base"
EXPECTED_SEMANTIC_REVISION = "d128750597153bb5987e10b1c3493a34e5a4502a"
EXPECTED_SEMANTIC_DIMENSIONS = 768
SEMANTIC_TEXT_VERSION = "v2"
MAX_JSON_BYTES = 256 * 1024
MAX_HOOK_BYTES = 256 * 1024
MAX_PROJECTS = 256
MAX_LATEST = 10
MAX_MODEL_PROFILES = 20
MAX_FAILURE_CODES = 20
SERVICE_STATE_VERSION = 1
SERVICE_STARTUP_TTL = 15.0
DEEP_CHECK_BUDGET_SECONDS = 5.0

REQUIRED_TABLES = {
    "entries",
    "entry_sources",
    "entries_fts",
    "observation_jobs",
    "observation_job_sources",
    "embedding_documents",
    "embedding_vectors",
    "embedding_jobs",
    "embedding_job_entries",
}


def _tool_version() -> str:
    """Read the package version without importing configuration or storage."""

    root = Path(__file__).resolve().parents[3]
    try:
        sys.path.insert(0, str(root))
        from codex_mem import __version__  # type: ignore

        return __version__ if isinstance(__version__, str) else "unknown"
    except Exception:
        return "unknown"
    finally:
        try:
            sys.path.remove(str(root))
        except ValueError:
            pass


def _canonical(path: str | os.PathLike[str]) -> str:
    try:
        return str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, TypeError, ValueError):
        return ""


def _data_dir(value: str | os.PathLike[str] | None) -> Path:
    if value is not None:
        raw = str(value)
    else:
        raw = os.environ.get("CODEX_MEM_HOME", "").strip()
        if not raw:
            raw = str(Path.home() / ".local" / "share" / "codex-mem")
    return Path(raw).expanduser().resolve(strict=False)


def _safe_atom(value: object, *, maximum: int = 128) -> str | None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        return None
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for char in value):
        return None
    return value


def _safe_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return value if parsed.tzinfo is not None else None


def _timestamp_epoch(value: object) -> float | None:
    checked = _safe_timestamp(value)
    if checked is None:
        return None
    try:
        return datetime.fromisoformat(checked.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _age(value: object, now: float) -> int | None:
    epoch = _timestamp_epoch(value)
    if epoch is None:
        return None
    return max(0, int(now - epoch))


def _numeric_age(value: object, now: float) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return max(0, int(now - float(value)))


def _error(code: str) -> str:
    return code if _safe_atom(code, maximum=64) else "diagnostic_error"


def _read_json(path: Path, maximum: int = MAX_JSON_BYTES) -> tuple[object | None, str | None]:
    try:
        if path.is_symlink() or not path.is_file():
            return None, "missing"
        if path.stat().st_size > maximum:
            return None, "too_large"
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        return None, "unreadable"


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    if db_path.is_symlink() or not db_path.is_file():
        raise FileNotFoundError
    uri = "file:" + quote(str(db_path), safe="/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0.5)
    connection.row_factory = sqlite3.Row
    return connection


def _rows(connection: sqlite3.Connection, query: str, parameters: Iterable[object] = ()) -> list[sqlite3.Row]:
    return connection.execute(query, tuple(parameters)).fetchall()


def _one(connection: sqlite3.Connection, query: str, parameters: Iterable[object] = ()) -> sqlite3.Row | None:
    return connection.execute(query, tuple(parameters)).fetchone()


def _value(row: sqlite3.Row | Mapping[str, Any] | None, key: str, default: object = None) -> object:
    if row is None:
        return default
    try:
        return row[key]  # type: ignore[index]
    except (IndexError, KeyError, TypeError):
        return default


def _project_counts(connection: sqlite3.Connection, project: str) -> dict[str, Any]:
    row = _one(
        connection,
        """
        SELECT COUNT(*) AS entries,
               COALESCE(SUM(CASE WHEN superseded_by IS NULL THEN 1 ELSE 0 END), 0) AS active_entries,
               COALESCE(SUM(CASE WHEN superseded_by IS NOT NULL THEN 1 ELSE 0 END), 0) AS superseded_entries,
               MIN(created_at) AS oldest_at, MAX(created_at) AS newest_at
        FROM entries WHERE project = ?
        """,
        (project,),
    )
    return {
        "entries": int(_value(row, "entries", 0) or 0),
        "active_entries": int(_value(row, "active_entries", 0) or 0),
        "superseded_entries": int(_value(row, "superseded_entries", 0) or 0),
        "oldest_at": _safe_timestamp(_value(row, "oldest_at")),
        "newest_at": _safe_timestamp(_value(row, "newest_at")),
    }


def _latest(connection: sqlite3.Connection, project: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in _rows(
        connection,
        """
        SELECT id, kind, created_at, updated_at, superseded_by IS NULL AS active
        FROM entries WHERE project = ? ORDER BY created_at DESC, id DESC LIMIT ?
        """,
        (project, MAX_LATEST),
    ):
        entry_id = _safe_atom(row["id"], maximum=64)
        kind = _safe_atom(row["kind"], maximum=64)
        if entry_id is None or kind is None:
            continue
        result.append(
            {
                "id": entry_id,
                "kind": kind,
                "created_at": _safe_timestamp(row["created_at"]),
                "updated_at": _safe_timestamp(row["updated_at"]),
                "active": bool(row["active"]),
            }
        )
    return result


def _observations(connection: sqlite3.Connection, project: str, now: float) -> dict[str, Any]:
    source_predicate = "(e.source LIKE 'hook:%')"
    last_observation = _one(
        connection,
        f"SELECT MAX(e.created_at) AS value FROM entries AS e WHERE e.project = ? AND {source_predicate}",
        (project,),
    )
    # Notes include explicit/manual records and processor-produced notes. Raw
    # hook entries are evidence, not curated notes.
    last_note = _one(
        connection,
        """SELECT MAX(created_at) AS value FROM entries
           WHERE project = ? AND (source IS NULL OR source NOT LIKE 'hook:%')
             AND kind NOT IN ('session', 'tool')""",
        (project,),
    )
    pending_row = _one(
        connection,
        f"""
        SELECT COUNT(*) AS value FROM entries AS e
        WHERE e.project = ? AND e.superseded_by IS NULL AND {source_predicate}
          AND NOT EXISTS (
            SELECT 1 FROM observation_job_sources AS links
            JOIN observation_jobs AS jobs ON jobs.id = links.job_id
            WHERE links.source_id = e.id AND jobs.project = e.project
              AND jobs.status IN ('processed', 'skipped', 'running', 'failed')
          )
        """,
        (project,),
    )
    failed_row = _one(
        connection,
        f"""
        SELECT COUNT(*) AS value FROM entries AS e
        WHERE e.project = ? AND e.superseded_by IS NULL AND {source_predicate}
          AND EXISTS (
            SELECT 1 FROM observation_job_sources AS links
            JOIN observation_jobs AS jobs ON jobs.id = links.job_id
            WHERE links.source_id = e.id AND jobs.project = e.project AND jobs.status = 'failed'
          )
        """,
        (project,),
    )
    return {
        "last_observation_at": _safe_timestamp(_value(last_observation, "value")),
        "last_note_at": _safe_timestamp(_value(last_note, "value")),
        "last_curated_note_at": _safe_timestamp(_value(last_note, "value")),
        "pending_observations": int(_value(pending_row, "value", 0) or 0),
        "failed_observations": int(_value(failed_row, "value", 0) or 0),
        "last_observation_age_seconds": _age(_value(last_observation, "value"), now),
        "last_note_age_seconds": _age(_value(last_note, "value"), now),
    }


def _observation_queue(
    connection: sqlite3.Connection,
    project: str,
    now: float,
    pending_observations: int = 0,
) -> dict[str, Any]:
    rows = _rows(
        connection,
        """
        SELECT status, COUNT(*) AS count, MIN(created_at) AS oldest_at,
               MAX(updated_at) AS last_progress_at, MAX(completed_at) AS completed_at
        FROM observation_jobs WHERE project = ? GROUP BY status
        """,
        (project,),
    )
    counts: dict[str, int] = {"running": 0, "failed": 0, "processed": 0, "skipped": 0}
    invalid = 0
    oldest: dict[str, object] = {}
    latest: dict[str, object] = {}
    last_progress: list[object] = []
    successful: list[object] = []
    profiles: dict[tuple[str, str], int] = {}
    failure_codes: dict[str, int] = {}
    for row in rows:
        status = row["status"] if row["status"] in counts else None
        if status is None:
            invalid += int(row["count"] or 0)
        else:
            counts[status] += int(row["count"] or 0)
            oldest[status] = row["oldest_at"]
            latest[status] = row["last_progress_at"]
            if row["last_progress_at"] is not None:
                last_progress.append(row["last_progress_at"])
            if status in {"processed", "skipped"} and row["completed_at"] is not None:
                successful.append(row["completed_at"])
    for row in _rows(
        connection,
        """SELECT model, reasoning_effort, COUNT(*) AS count FROM observation_jobs
           WHERE project = ? GROUP BY model, reasoning_effort""",
        (project,),
    ):
        model = _safe_atom(row["model"])
        effort = _safe_atom(row["reasoning_effort"])
        if model is not None and effort is not None:
            profiles[(model, effort)] = int(row["count"] or 0)
    for row in _rows(
        connection,
        """SELECT error_code, COUNT(*) AS count FROM observation_jobs
           WHERE project = ? AND status = 'failed' GROUP BY error_code""",
        (project,),
    ):
        code = _safe_atom(row["error_code"], maximum=64)
        if code is not None:
            failure_codes[code] = int(row["count"] or 0)

    stale_running_rows = _rows(
        connection,
        """SELECT lease_expires_at FROM observation_jobs
           WHERE project = ? AND status = 'running' AND lease_expires_at IS NOT NULL""",
        (project,),
    )
    stale_running = sum(
        1 for row in stale_running_rows if (_age(row["lease_expires_at"], now) or 0) > 0
    )
    last_successful = max(
        (value for value in successful if _timestamp_epoch(value) is not None),
        key=lambda value: _timestamp_epoch(value) or 0,
        default=None,
    )
    last_progress_value = max(
        (value for value in last_progress if _timestamp_epoch(value) is not None),
        key=lambda value: _timestamp_epoch(value) or 0,
        default=None,
    )
    status = "idle"
    if invalid:
        status = "unavailable"
    elif stale_running:
        status = "stale"
    elif counts["running"]:
        status = "in_progress"
    elif counts["failed"]:
        status = "quarantined"
    elif counts["processed"] or counts["skipped"]:
        status = "healthy"
    result: dict[str, Any] = {
        "status": status,
        "counts": {**counts, "invalid": invalid},
        "pending_jobs": 0,
        "pending_sources": pending_observations,
        "quarantined_jobs": counts["failed"],
        "failure_codes": [
            {"code": code, "jobs": count}
            for code, count in sorted(failure_codes.items())[:MAX_FAILURE_CODES]
        ],
        "oldest_running_at": _safe_timestamp(oldest.get("running")),
        "oldest_failed_at": _safe_timestamp(oldest.get("failed")),
        "latest_failed_at": _safe_timestamp(latest.get("failed")),
        "oldest_running_age_seconds": _age(oldest.get("running"), now),
        "oldest_failed_age_seconds": _age(oldest.get("failed"), now),
        "latest_failed_age_seconds": _age(latest.get("failed"), now),
        "last_progress_at": _safe_timestamp(last_progress_value),
        "last_successful_processing_at": _safe_timestamp(last_successful),
        "last_progress_age_seconds": _age(last_progress_value, now),
        "last_successful_processing_age_seconds": _age(last_successful, now),
        "stale_running": stale_running,
        "model_profiles": [
            {"model": model, "reasoning_effort": effort, "jobs": count}
            for (model, effort), count in sorted(profiles.items())[:MAX_MODEL_PROFILES]
        ],
        "model_expectation": {
            "model": EXPECTED_PROCESSOR_MODEL,
            "reasoning_effort": EXPECTED_PROCESSOR_EFFORT,
            "recorded_match": None
            if not profiles
            else all(
                model == EXPECTED_PROCESSOR_MODEL and effort == EXPECTED_PROCESSOR_EFFORT
                for model, effort in profiles
            ),
        },
    }
    return result


def _semantic_index(connection: sqlite3.Connection, project: str, enabled: bool) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model": EXPECTED_SEMANTIC_MODEL,
        "revision": EXPECTED_SEMANTIC_REVISION,
        "dimensions": EXPECTED_SEMANTIC_DIMENSIONS,
        "text_version": SEMANTIC_TEXT_VERSION,
        "runtime_status": "unknown",
        "runtime_evidence": "model_not_loaded_by_read_only_check",
    }
    if not enabled:
        base.update({"status": "disabled", "indexed": 0, "pending": 0, "stale": 0})
        return base
    total_row = _one(
        connection,
        "SELECT COUNT(*) AS count FROM entries WHERE project = ? AND superseded_by IS NULL AND COALESCE(source, '') NOT GLOB 'hook:*'",
        (project,),
    )
    total_entries = int(_value(total_row, "count", 0) or 0)
    raw_row = _one(connection, "SELECT COUNT(*) AS count FROM entries WHERE project = ? "
                   "AND superseded_by IS NULL AND source GLOB 'hook:*'", (project,))
    base["raw_observations_excluded"] = int(_value(raw_row, "count", 0) or 0)
    sample_limit = 5000
    rows = _rows(
        connection,
        """
        SELECT e.id, d.content_hash AS document_hash, d.text_version,
               v.content_hash AS vector_hash
        FROM entries AS e
        LEFT JOIN embedding_documents AS d ON d.entry_id = e.id AND d.project = e.project
        LEFT JOIN embedding_vectors AS v ON v.entry_id = e.id AND v.project = e.project
          AND v.model = ? AND v.revision = ? AND v.dimensions = ?
        WHERE e.project = ? AND e.superseded_by IS NULL AND COALESCE(e.source, '') NOT GLOB 'hook:*'
        LIMIT ?
        """,
        (EXPECTED_SEMANTIC_MODEL, EXPECTED_SEMANTIC_REVISION, EXPECTED_SEMANTIC_DIMENSIONS, project, sample_limit),
    )
    indexed = pending = stale = 0
    for row in rows:
        vector_hash = row["vector_hash"]
        document_hash = row["document_hash"]
        if vector_hash is None:
            pending += 1
        elif document_hash is None or row["text_version"] != SEMANTIC_TEXT_VERSION:
            pending += 1
        elif vector_hash != document_hash:
            stale += 1
        else:
            indexed += 1
    jobs = {"jobs": 0, "running": 0, "failed": 0, "completed": 0}
    for row in _rows(
        connection,
        """SELECT status, COUNT(*) AS count FROM embedding_jobs
           WHERE project = ? AND model = ? AND revision = ? AND dimensions = ? GROUP BY status""",
        (project, EXPECTED_SEMANTIC_MODEL, EXPECTED_SEMANTIC_REVISION, EXPECTED_SEMANTIC_DIMENSIONS),
    ):
        jobs["jobs"] += int(row["count"] or 0)
        if row["status"] in {"running", "failed", "completed"}:
            jobs[row["status"]] += int(row["count"] or 0)
    sampled = total_entries > sample_limit
    status = "unknown" if sampled else "idle" if not rows else "ready" if pending == 0 and stale == 0 else "stale" if stale else "pending"
    base.update({"status": status, "indexed": indexed, "pending": pending, "stale": stale,
                 "total_active_entries": total_entries, "sampled": sampled,
                 "coverage": "bounded_sample" if sampled else "complete", "jobs": jobs})
    return base


def _config(data_dir: Path) -> tuple[dict[str, Any], list[str]]:
    path = data_dir / "config.json"
    raw, error = _read_json(path)
    defaults: dict[str, Any] = {
        "capture_enabled": True,
        "capture_tools": True,
        "processor_enabled": True,
        "service_enabled": True,
        "semantic_enabled": True,
        "usage_enabled": True,
        "capture_scope": "selected",
        "context_chars": 6000,
        "included_projects": 0,
        "excluded_projects": 0,
    }
    if error == "missing":
        defaults.update({"status": "default", "present": False, "valid": True})
        return defaults, []
    if error is not None or not isinstance(raw, Mapping):
        defaults.update({"status": "invalid", "present": True, "valid": False})
        return defaults, [_error("config_invalid")]
    result = dict(defaults)
    valid = True
    for key in ("capture_enabled", "capture_tools", "processor_enabled", "service_enabled", "semantic_enabled", "usage_enabled"):
        value = raw.get(key, defaults[key])
        if not isinstance(value, bool):
            valid = False
        else:
            result[key] = value
    scope = raw.get("capture_scope", defaults["capture_scope"])
    if scope not in {"selected", "all", "manual"}:
        valid = False
    else:
        result["capture_scope"] = scope
    context_chars = raw.get("context_chars", defaults["context_chars"])
    if not isinstance(context_chars, int) or isinstance(context_chars, bool) or not 1 <= context_chars <= 6000:
        valid = False
    else:
        result["context_chars"] = context_chars
    for key in ("included_projects", "excluded_projects"):
        value = raw.get(key, [])
        if not isinstance(value, list):
            valid = False
        else:
            result[key] = len(value)
    result.update({"status": "valid" if valid else "invalid", "present": True, "valid": valid})
    return result, [] if valid else [_error("config_invalid")]


def _hook_report() -> tuple[dict[str, Any], list[str]]:
    root = Path(__file__).resolve().parents[3]
    path = root / "hooks" / "hooks.json"
    raw, error = _read_json(path, MAX_HOOK_BYTES)
    if error != "missing" and error is None and isinstance(raw, Mapping):
        hooks = raw.get("hooks")
        if isinstance(hooks, Mapping):
            events = sorted(_safe_atom(name, maximum=64) for name in hooks if _safe_atom(name, maximum=64))
            command_count = 0
            for value in hooks.values():
                if not isinstance(value, list):
                    continue
                for matcher in value:
                    if isinstance(matcher, Mapping) and isinstance(matcher.get("hooks"), list):
                        command_count += sum(
                            1 for item in matcher["hooks"] if isinstance(item, Mapping) and item.get("type") == "command"
                        )
            return {
                "status": "unknown",
                "trust": "unknown",
                "execution_receipt": "unknown",
                "definitions": {
                    "present": True,
                    "valid": True,
                    "event_count": len(events),
                    "command_count": command_count,
                    "events": events,
                },
            }, []
    code = "hook_definitions_missing" if error == "missing" else "hook_definitions_unavailable"
    return {
        "status": "unknown",
        "trust": "unknown",
        "execution_receipt": "unknown",
        "definitions": {"present": error != "missing", "valid": False},
    }, [_error(code)]


def _lock_held(path: Path) -> bool | None:
    """Inspect the service flock without creating or writing its lock file."""

    if fcntl is None:
        return None
    try:
        if path.is_symlink() or not path.is_file():
            return False
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError):
        return None


def _service_report(data_dir: Path, project: str, now: float) -> tuple[dict[str, Any], list[str], list[str]]:
    state_raw, state_error = _read_json(data_dir / "service-state.json")
    pid_raw, pid_error = _read_json(data_dir / "service.pid", maximum=4096)
    startup_raw, startup_error = _read_json(data_dir / ".service-starting.json", maximum=4096)
    errors: list[str] = []
    if state_error not in {None, "missing"}:
        errors.append(_error("service_state_invalid"))
    if pid_error not in {None, "missing"} or startup_error not in {None, "missing"}:
        errors.append(_error("service_lifecycle_unavailable"))
    state = state_raw if isinstance(state_raw, Mapping) and state_raw.get("version") == SERVICE_STATE_VERSION else None
    if state_raw is not None and state is None:
        errors.append(_error("service_state_invalid"))
    projects = state.get("projects", {}) if state else {}
    if not isinstance(projects, Mapping):
        projects = {}
        errors.append(_error("service_state_invalid"))
    elif len(projects) > MAX_PROJECTS:
        errors.append(_error("service_state_bounded"))
        projects = dict(list(projects.items())[:MAX_PROJECTS])
    record = projects.get(project)
    record_out: dict[str, Any] = {"queued": False}
    if isinstance(record, Mapping):
        record_out = {
            "queued": True,
            "blocked": bool(record.get("blocked")) if isinstance(record.get("blocked"), bool) else None,
            "parked": _safe_atom(record.get("parked"), maximum=32) if record.get("parked") is not None else None,
            "attempts": record.get("attempts") if isinstance(record.get("attempts"), int) else None,
            "due_at": record.get("due_at") if isinstance(record.get("due_at"), (int, float)) else None,
            "due_age_seconds": _numeric_age(record.get("due_at"), now),
            "inflight": record.get("inflight_generation") is not None,
            "inflight_until": record.get("inflight_until") if isinstance(record.get("inflight_until"), (int, float)) else None,
            "inflight_age_seconds": _numeric_age(record.get("inflight_until"), now),
            "last_code": _safe_atom(record.get("last_code"), maximum=64) if record.get("last_code") else None,
        }
    owner = state.get("owner") if state else None
    pid_file = pid_raw if isinstance(pid_raw, Mapping) else None
    pid = pid_file.get("pid") if pid_file else None
    alive: bool | None = None
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = None
        except OSError:
            alive = False
    lock_held = _lock_held(data_dir / "service.pid")
    owner_match = (
        isinstance(owner, Mapping)
        and isinstance(pid_file, Mapping)
        and owner.get("pid") == pid_file.get("pid")
        and owner.get("nonce") == pid_file.get("nonce")
    )
    starting = False
    if isinstance(startup_raw, Mapping) and isinstance(startup_raw.get("started_at"), (int, float)):
        starting = now - float(startup_raw["started_at"]) <= SERVICE_STARTUP_TTL and alive is not False
    visibility_unknown = (
        owner_match and alive is not False and lock_held is not False
        and (lock_held is None or alive is None)
    )
    liveness = (
        "running" if owner_match and lock_held is True and alive is True
        else "unknown" if visibility_unknown
        else "starting" if starting
        else "stale" if owner_match and (lock_held is False or alive is False)
        else "stopped"
    )
    service_status = "healthy" if liveness == "running" else "idle" if liveness == "stopped" else liveness
    known_projects = []
    for key in projects:
        if not isinstance(key, str):
            continue
        canonical = _canonical(key)
        if canonical and canonical == key:
            known_projects.append(canonical)
    return {
        "status": service_status,
        "worker_liveness": liveness,
        "scheduling": {
            "queued_projects": len(known_projects),
            "blocked_projects": sum(1 for value in projects.values() if isinstance(value, Mapping) and value.get("blocked") is True),
            "stop_requested": bool(state.get("stop_requested")) if state else False,
            "selected_project": record_out,
        },
        "queue_metadata": record_out,
    }, errors, sorted(known_projects)[:MAX_PROJECTS]


def _usage_report(
    connection: sqlite3.Connection,
    project: str,
    tables: set[str],
    now: float,
    enabled: bool,
) -> dict[str, Any]:
    """Read project accounting totals without initializing the optional ledger."""

    required = {"usage_sessions", "usage_events"}
    base: dict[str, Any] = {
        "enabled": enabled,
        "status": "missing",
        "collection_liveness": "unknown",
    }
    if not required.intersection(tables):
        return base
    if not required.issubset(tables):
        return {**base, "status": "unavailable"}
    try:
        sessions = _one(
            connection,
            """SELECT COUNT(*) AS threads, COUNT(DISTINCT session_id) AS root_sessions,
                      COALESCE(SUM(parent_thread_id IS NOT NULL), 0) AS child_threads
               FROM usage_sessions WHERE project = ?""",
            (project,),
        )
        events = _one(
            connection,
            """SELECT COUNT(*) AS events, COUNT(DISTINCT e.thread_id) AS threads_with_events,
                      COALESCE(SUM(e.input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(e.cached_input_tokens), 0) AS cached_input_tokens,
                      COALESCE(SUM(e.cache_write_input_tokens), 0) AS cache_write_input_tokens,
                      COALESCE(SUM(e.output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(e.reasoning_output_tokens), 0) AS reasoning_output_tokens,
                      COALESCE(SUM(e.total_tokens), 0) AS total_tokens,
                      COALESCE(SUM(e.quality = 'response_exact'), 0) AS exact_events,
                      COALESCE(SUM(e.quality = 'legacy_cumulative_delta'), 0) AS legacy_events,
                      MIN(e.recorded_at) AS first_event_at, MAX(e.recorded_at) AS last_event_at
               FROM usage_events AS e JOIN usage_sessions AS s ON s.thread_id = e.thread_id
               WHERE s.project = ?""",
            (project,),
        )
        counts = {
            key: int(_value(events, key, 0) or 0)
            for key in ("events", "threads_with_events", "exact_events", "legacy_events")
        }
        counts["other_events"] = counts["events"] - counts["exact_events"] - counts["legacy_events"]
        return {
            **base,
            "status": "ready" if counts["events"] else "empty",
            **{key: int(_value(sessions, key, 0) or 0) for key in ("threads", "root_sessions", "child_threads")},
            **counts,
            "tokens": {
                key: int(_value(events, key, 0) or 0)
                for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
            },
            "first_event_at": _safe_timestamp(_value(events, "first_event_at")),
            "last_event_at": _safe_timestamp(_value(events, "last_event_at")),
            "last_event_age_seconds": _age(_value(events, "last_event_at"), now),
        }
    except (sqlite3.Error, TypeError, ValueError, OverflowError):
        return {**base, "status": "unavailable"}


def _observer_usage_report(connection: sqlite3.Connection, project: str) -> dict[str, Any]:
    """Share the read-only accounting query without opening or migrating Store."""

    plugin_root = str(Path(__file__).resolve().parents[3])
    if plugin_root not in sys.path:
        sys.path.insert(0, plugin_root)
    try:
        from codex_mem.observer_usage_store import observer_usage_summary
        return observer_usage_summary(connection, project)
    except (ImportError, sqlite3.Error, TypeError, ValueError, OverflowError):
        return {"status": "unavailable", "project": project, "scope": "project"}


def _db_report(
    data_dir: Path,
    projects: list[str],
    deep: bool,
    now: float,
    semantic_enabled: bool = True,
    usage_enabled: bool = True,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]:
    db_path = data_dir / "memory.sqlite3"
    base: dict[str, Any] = {
        "status": "missing",
        "db_path": str(db_path),
        "database_bytes": None,
        "schema_version": None,
        "integrity": {"status": "skipped" if not deep else "unavailable"},
    }
    errors: list[str] = []
    per_project: dict[str, dict[str, Any]] = {}
    try:
        base["database_bytes"] = db_path.stat().st_size
    except OSError:
        pass
    if not db_path.exists():
        return base, per_project, errors
    try:
        connection = _connect_readonly(db_path)
    except FileNotFoundError:
        return base, per_project, [_error("database_unreadable")]
    except (OSError, sqlite3.Error):
        return {**base, "status": "unavailable"}, per_project, [_error("database_unreadable")]
    try:
        try:
            version_row = _one(connection, "PRAGMA user_version")
            version = int(version_row[0]) if version_row else None
            base["schema_version"] = version
        except (TypeError, ValueError, sqlite3.Error):
            version = None
        try:
            names = {
                str(row["name"])
                for row in _rows(connection, "SELECT name FROM sqlite_master WHERE type IN ('table','shadow')")
            }
        except sqlite3.Error:
            base["status"] = "unavailable"
            errors.append(_error("database_unreadable"))
            return base, per_project, errors
        if version not in {3, EXPECTED_SCHEMA_VERSION} or not REQUIRED_TABLES.issubset(names):
            base["status"] = "schema_mismatch"
            errors.append(_error("schema_invalid"))
            return base, per_project, errors
        base["status"] = "healthy"
        if deep:
            try:
                deadline = time.monotonic() + DEEP_CHECK_BUDGET_SECONDS
                connection.set_progress_handler(
                    lambda: 1 if time.monotonic() >= deadline else 0, 1000
                )
                checks = _rows(connection, "PRAGMA quick_check")
                connection.set_progress_handler(None, 0)
                base["integrity"] = {"status": "healthy" if checks and all(row[0] == "ok" for row in checks) else "failed"}
                if base["integrity"]["status"] != "healthy":
                    errors.append(_error("integrity_check_failed"))
            except sqlite3.Error:
                connection.set_progress_handler(None, 0)
                base["integrity"] = {"status": "failed"}
                errors.append(_error("integrity_check_timeout" if time.monotonic() >= deadline else "integrity_check_failed"))
        for project in projects:
            try:
                counts = _project_counts(connection, project)
                observations = _observations(connection, project, now)
                queue = _observation_queue(
                    connection, project, now, int(observations["pending_observations"])
                )
                per_project[project] = {
                    **counts,
                    "latest": _latest(connection, project),
                    **observations,
                    "observation_queue": queue,
                    "semantic": _semantic_index(connection, project, semantic_enabled),
                    "usage": _usage_report(connection, project, names, now, usage_enabled),
                    "observer_usage": _observer_usage_report(connection, project),
                }
                if per_project[project]["usage"]["status"] == "unavailable":
                    errors.append(_error("usage_metadata_unavailable"))
            except (sqlite3.Error, TypeError, ValueError):
                per_project[project] = {"status": "unavailable"}
                errors.append(_error("metadata_unavailable"))
        return base, per_project, errors
    finally:
        connection.close()


def collect_report(
    project: str | os.PathLike[str] | None = None,
    *,
    all_projects: bool = False,
    data_dir: str | os.PathLike[str] | None = None,
    deep: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Collect one bounded report without creating any path or opening a writer."""

    current_time = time.time() if now is None else float(now)
    selected = _canonical(project or Path.cwd())
    base_dir = _data_dir(data_dir)
    config, config_errors = _config(base_dir)
    hooks, hook_errors = _hook_report()
    service_global, service_errors, service_projects = _service_report(base_dir, selected, current_time)

    candidate_projects: list[str] = []
    db_path = base_dir / "memory.sqlite3"
    if all_projects and db_path.is_file() and not db_path.is_symlink():
        try:
            connection = _connect_readonly(db_path)
            try:
                for row in _rows(connection, "SELECT DISTINCT project FROM entries ORDER BY project LIMIT ?", (MAX_PROJECTS,)):
                    value = _canonical(row["project"])
                    if value:
                        candidate_projects.append(value)
                if _one(connection, "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'usage_sessions'"):
                    for row in _rows(connection, "SELECT DISTINCT project FROM usage_sessions ORDER BY project LIMIT ?", (MAX_PROJECTS,)):
                        value = _canonical(row["project"])
                        if value:
                            candidate_projects.append(value)
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            pass
    if all_projects:
        candidate_projects.extend(service_projects)
        candidate_projects = sorted(set(candidate_projects))[:MAX_PROJECTS]
        if not candidate_projects:
            candidate_projects = [selected] if selected else []
    else:
        candidate_projects = [selected] if selected else []

    db, project_data, db_errors = _db_report(
        base_dir,
        candidate_projects,
        deep,
        current_time,
        config.get("semantic_enabled") is not False,
        config.get("usage_enabled") is not False,
    )
    service_by_project: dict[str, dict[str, Any]] = {selected: service_global}
    for path in candidate_projects:
        if path not in service_by_project:
            report, more_errors, _ = _service_report(base_dir, path, current_time)
            service_by_project[path] = report
            service_errors.extend(more_errors)
    reports: list[dict[str, Any]] = []
    for path in candidate_projects:
        value = project_data.get(path, {"entries": 0, "active_entries": 0, "superseded_entries": 0, "latest": []})
        stored_queue = value.get("observation_queue", {})
        queue = dict(stored_queue) if isinstance(stored_queue, Mapping) else {}
        service = service_by_project.get(path, service_global)
        service_queue = service.get("queue_metadata", {}) if isinstance(service, Mapping) else {}
        service_blocked = isinstance(service_queue, Mapping) and service_queue.get("blocked") is True
        if service_blocked:
            queue["status"] = "blocked"
        failed_observations = int(value.get("failed_observations", 0) or 0)
        local_status = "idle"
        if db["status"] == "unavailable" or value.get("status") == "unavailable":
            local_status = "unavailable"
        elif db["status"] == "schema_mismatch":
            local_status = "unavailable"
        elif config.get("valid") is False:
            local_status = "degraded"
        elif service_blocked:
            local_status = "blocked"
        elif queue.get("status") == "unavailable":
            local_status = "unavailable"
        elif queue.get("status") == "stale":
            local_status = "stale"
        elif queue.get("status") == "in_progress":
            local_status = "in_progress"
        elif queue.get("status") == "quarantined":
            local_status = "quarantined"
        elif (
            isinstance(service_queue, Mapping)
            and service_queue.get("queued") is True
            and (
                (isinstance(service_queue.get("due_age_seconds"), int) and service_queue["due_age_seconds"] > 0)
                or (service_queue.get("inflight") is True and isinstance(service_queue.get("inflight_age_seconds"), int) and service_queue["inflight_age_seconds"] > 0)
            )
            and service.get("worker_liveness") in {"stopped", "stale"}
        ):
            local_status = "stale"
        elif isinstance(value.get("semantic"), Mapping) and value["semantic"].get("jobs", {}).get("failed", 0) > 0:
            local_status = "degraded"
        elif isinstance(value.get("usage"), Mapping) and value["usage"].get("status") == "unavailable":
            local_status = "degraded"
        elif int(value.get("active_entries", 0) or 0) > 0:
            local_status = "healthy"
        reports.append(
            {
                "path": path,
                "exists": Path(path).exists(),
                "status": local_status,
                "end_to_end": "unknown",
                "evidence": {"source": "local_metadata", "observed": True},
                "records": {key: value.get(key) for key in ("entries", "active_entries", "superseded_entries", "oldest_at", "newest_at", "latest")},
                "observations": {
                    **{key: value.get(key) for key in ("last_observation_at", "last_observation_age_seconds", "pending_observations")},
                    "failed_observations": failed_observations,
                    "blocked_observations": failed_observations if service_blocked else 0,
                    "quarantined_observations": 0 if service_blocked else failed_observations,
                },
                "notes": {key: value.get(key) for key in ("last_note_at", "last_curated_note_at", "last_note_age_seconds")},
                "processing": {
                    "last_successful_processing_at": value.get("observation_queue", {}).get("last_successful_processing_at") if isinstance(value.get("observation_queue"), Mapping) else None,
                    "last_successful_processing_age_seconds": value.get("observation_queue", {}).get("last_successful_processing_age_seconds") if isinstance(value.get("observation_queue"), Mapping) else None,
                    "model_profiles": value.get("observation_queue", {}).get("model_profiles", []) if isinstance(value.get("observation_queue"), Mapping) else [],
                },
                "observation_queue": queue or {"status": "idle"},
                "semantic": value.get("semantic", {"status": "unknown"}),
                "usage": value.get("usage", {
                    "enabled": config.get("usage_enabled") is not False,
                    "status": "missing" if db["status"] == "missing" else "unavailable",
                    "collection_liveness": "unknown",
                }),
                "observer_usage": value.get("observer_usage", {"status": "unavailable", "scope": "project"}),
                "service": service,
            }
        )

    errors = sorted(set(config_errors + hook_errors + service_errors + db_errors))
    overall = "idle" if db["status"] == "missing" else "healthy"
    if any(item["status"] == "unavailable" for item in reports) or db["status"] == "unavailable":
        overall = "unavailable"
    elif any(item["status"] == "blocked" for item in reports):
        overall = "blocked"
    elif any(item["status"] == "stale" for item in reports):
        overall = "stale"
    elif any(item["status"] == "in_progress" for item in reports):
        overall = "in_progress"
    elif any(item["status"] == "quarantined" for item in reports):
        overall = "quarantined"
    elif any(item["status"] == "degraded" for item in reports):
        overall = "degraded"
    elif service_errors:
        overall = "degraded"
    return {
        "schema_version": REPORT_VERSION,
        "tool_version": _tool_version(),
        "status": overall,
        "end_to_end": "unknown",
        "scope": "all-projects" if all_projects else "project",
        "capture_scope": config.get("capture_scope", "selected"),
        "evidence": {
            "local": "observed",
            "native": "unknown",
            "native_trust": "unknown",
            "native_execution": "unknown",
        },
        "data_dir": str(base_dir),
        "configuration": config,
        "storage": db,
        "hooks": hooks,
        "projects": reports,
        "errors": errors,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Codex Mem health diagnostics")
    parser.add_argument("--project", help="absolute project path (defaults to cwd)")
    parser.add_argument("--all-projects", action="store_true", help="scan bounded metadata for every recorded project")
    parser.add_argument("--data-dir", help="memory data directory; otherwise CODEX_MEM_HOME/default")
    parser.add_argument("--deep", action="store_true", help="run SQLite PRAGMA quick_check")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    report = collect_report(
        arguments.project,
        all_projects=arguments.all_projects,
        data_dir=arguments.data_dir,
        deep=arguments.deep,
    )
    print(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
