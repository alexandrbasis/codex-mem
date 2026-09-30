"""Read a small, safe summary of recent hook traces without changing local state."""

from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any
import uuid

from .hook_diagnostics import HOOK_EVENTS, LOG_BACKUPS, MAX_RECORD_BYTES, log_path

_TAIL_BYTES = 256 * 1024
_RUN_LIMIT = 50
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,120}\Z")
_COMPONENTS = frozenset({"supervisor", "worker", "processor", "process_hook", "hook"})
_STATUSES = frozenset({"ok", "degraded", "fallback", "failed", "cancelled", "invalid_input", "skipped"})
_BAD_STATUSES = _STATUSES - {"ok", "skipped"}


def _token(value: object) -> str | None:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _uuid(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 36:
        return None
    try:
        parsed = uuid.UUID(value)
        return str(parsed) if str(parsed) == value else None
    except ValueError:
        return None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**31 else None


def _row(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        return None
    run_id, component = _uuid(value.get("run_id")), value.get("component")
    event, stage = _token(value.get("event")), _token(value.get("stage"))
    stamp = value.get("timestamp")
    if not run_id or not isinstance(component, str) or component not in _COMPONENTS or not event or not stage or not isinstance(stamp, str) or len(stamp) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None:
            return None
    except ValueError:
        return None
    elapsed = value.get("elapsed_ms")
    if (_integer(value.get("pid")) is None or not isinstance(elapsed, (int, float))
            or isinstance(elapsed, bool) or not math.isfinite(elapsed) or elapsed < 0
            or elapsed > 2**53):
        return None
    result: dict[str, Any] = {"run_id": run_id, "component": component, "event": event,
                              "stage": stage, "timestamp": stamp, "elapsed_ms": elapsed}
    for key in ("declared_hook_event", "hook_event"):
        name = value.get(key)
        if isinstance(name, str) and (name in HOOK_EVENTS or (key == "hook_event" and name == "unknown")):
            result[key] = name
    for key in ("session_id", "turn_id"):
        identifier = _uuid(value.get(key))
        if identifier:
            result[key] = identifier
    if event == "completed":
        status = value.get("status")
        if not isinstance(status, str) or status not in _STATUSES or _integer(value.get("error_count")) is None:
            return None
        result["status"] = status
        result["error_count"] = value["error_count"]
        code = _token(value.get("code"))
        if code:
            result["code"] = code
    elif event == "error":
        code = _token(value.get("code"))
        if code:
            result["code"] = code
        detail = value.get("error")
        if isinstance(detail, dict):
            error: dict[str, Any] = {}
            exception_type = _token(detail.get("exception_type"))
            if exception_type:
                error["exception_type"] = exception_type
            failed_stage = _token(detail.get("failed_stage"))
            if failed_stage:
                error["failed_stage"] = failed_stage
            for key in ("errno", "sqlite_errorcode"):
                number = _integer(detail.get(key))
                if number is not None:
                    error[key] = number
            sqlite_name = detail.get("sqlite_errorname")
            if isinstance(sqlite_name, str) and re.fullmatch(r"SQLITE_[A-Z_]{1,80}", sqlite_name):
                error["sqlite_errorname"] = sqlite_name
            if error:
                result["error"] = error
    return result


def _read_tail(path: Path) -> tuple[list[dict[str, Any]], bool, bool]:
    """Return rows, gap flag, and whether a regular log was available."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return [], False, False
    except (OSError, ValueError):
        return [], True, False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return [], True, False
        start = max(0, info.st_size - _TAIL_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        raw = os.read(fd, _TAIL_BYTES)
    except OSError:
        return [], True, False
    finally:
        os.close(fd)
    gap = start > 0 or (bool(raw) and not raw.endswith(b"\n"))
    lines = raw.split(b"\n")
    if start:
        lines = lines[1:]
    if raw and not raw.endswith(b"\n"):
        lines = lines[:-1]
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line:
            continue
        if len(line) > MAX_RECORD_BYTES:
            gap = True
            continue
        try:
            parsed = _row(json.loads(line))
        except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError, OverflowError):
            parsed = None
        if parsed is None:
            gap = True
        else:
            rows.append(parsed)
    return rows, gap, True


def recent_hook_logs(data_dir: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    path = log_path(data_dir).absolute()
    all_rows: list[dict[str, Any]] = []
    gap = False
    available = False
    active_available = False
    for index in range(LOG_BACKUPS, -1, -1):
        current = Path(f"{path}.{index}") if index else path
        rows, partial, exists = _read_tail(current)
        all_rows.extend(rows)
        gap |= partial
        available |= exists
        if index == 0:
            active_available = exists
    if available and not active_available:
        gap = True
    runs: dict[str, dict[str, Any]] = {}
    for row in all_rows:
        run = runs.setdefault(row["run_id"], {"run_id": row["run_id"], "first_seen": row["timestamp"], "last_seen": row["timestamp"], "outcomes": {}, "completion_codes": {}, "last_stages": {}, "elapsed_ms": {}, "errors": [], "started_components": set()})
        run["last_seen"] = row["timestamp"]
        run["elapsed_ms"][row["component"]] = row["elapsed_ms"]
        for key in ("declared_hook_event", "hook_event", "session_id", "turn_id"):
            if key in row:
                run[key] = row[key]
        if row["event"] == "started":
            run["started_components"].add(row["component"])
        if row["event"] in ("stage_started", "stage_interrupted"):
            run["last_stages"][row["component"]] = row["stage"]
        if row["event"] == "completed":
            run["outcomes"][row["component"]] = row["status"]
            if "code" in row:
                run["completion_codes"][row["component"]] = row["code"]
            if row["error_count"] and row["status"] == "ok":
                gap = True
        if row["event"] == "error":
            run["errors"].append({key: row[key] for key in ("component", "stage", "code", "error") if key in row})
            failed_stage = row.get("error", {}).get("failed_stage")
            if failed_stage:
                run["last_stages"][row["component"]] = failed_stage
    selected = list(runs.values())[-_RUN_LIMIT:]
    failures: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    for run in reversed(selected):
        base = {"run_id": run["run_id"], "first_seen": run["first_seen"], "last_seen": run["last_seen"], "outcomes": run["outcomes"], "completion_codes": run["completion_codes"], "last_stages": run["last_stages"], "elapsed_ms": run["elapsed_ms"]}
        base.update({key: run[key] for key in ("declared_hook_event", "hook_event", "session_id", "turn_id") if key in run})
        if run["errors"] or any(status in _BAD_STATUSES for status in run["outcomes"].values()):
            failures.append({**base, "errors": run["errors"][-5:]})
        if (not run["outcomes"] or ("supervisor" in run["started_components"] and "supervisor" not in run["outcomes"])):
            incomplete.append(base)
    return {
        "status": "partial" if gap or incomplete else ("available" if available else "unknown"),
        "path": str(path), "sampled_runs": len(selected), "failures": len(failures),
        "incomplete": len(incomplete), "recent_failures": failures[:10],
        "recent_incomplete": incomplete[:5],
    }
