"""Content-free, per-attempt failure receipts for observation maintenance."""

from __future__ import annotations

import sqlite3
import errno
from collections.abc import Mapping
from typing import Any


# Fixed vocabulary only. Never save model text or exception messages here.
INVALID_RESPONSE_REASONS = frozenset({
    "invalid_message_phase", "invalid_message_text", "missing_final_message",
    "multiple_final_messages", "invalid_json", "invalid_output_shape",
    "invalid_runner_receipt", "invalid_runner_evidence", "worker_id_mismatch",
    "turn_not_completed", "invalid_note_shape", "invalid_source_ids",
    "unknown_source_handle", "invalid_disposition", "too_many_notes",
    "missing_required_summary", "skipped_with_content", "processed_without_content",
    "invalid_source_batch", "invalid_observation_metadata", "source_attribution_conflict",
    "invalid_summary_shape", "invalid_summary_attribution", "future_summary_source",
    "invalid_summary_text", "invalid_summary_metadata", "empty_session_summary", "invalid_text", "invalid_tags",
    "jev_quality_rejected", "jev_quality_uncertain", "jev_quality_unavailable", "jev_quality_input_limit",
})
RUNNER_FAILURE_REASONS = frozenset({
    "jev_filter_failure", "jev_filter_credentials", "jev_filter_timeout",
    "jev_filter_transport", "jev_filter_invalid_response", "jev_filter_invalid_input",
    "native_turn_failed", "native_turn_cancelled", "native_rate_limit", "native_auth",
    "native_context_limit", "native_server_error", "native_connection_error",
    "native_usage_limit", "native_bad_request", "native_policy",
})
_FAILURE_CODES = frozenset({
    "invalid_request", "invalid_response", "model_mismatch", "model_unavailable", "protocol_error",
    "rerouted", "runner_failure", "runner_unavailable", "storage_failure", "lease_expired",
    "timeout", "tool_called", "tools_available",
})

# SQLite result codes are stable, but older Python sqlite3 modules do not
# export their names. Keep hook imports working on the system interpreter.
_STORAGE_SQLITE_REASONS = {
    getattr(sqlite3, "SQLITE_BUSY", 5): "sqlite_busy", getattr(sqlite3, "SQLITE_LOCKED", 6): "sqlite_locked",
    getattr(sqlite3, "SQLITE_CORRUPT", 11): "sqlite_corrupt", getattr(sqlite3, "SQLITE_NOTADB", 26): "sqlite_notadb",
    getattr(sqlite3, "SQLITE_READONLY", 8): "sqlite_readonly", getattr(sqlite3, "SQLITE_FULL", 13): "sqlite_full",
    getattr(sqlite3, "SQLITE_IOERR", 10): "sqlite_io", getattr(sqlite3, "SQLITE_CANTOPEN", 14): "sqlite_cantopen",
    getattr(sqlite3, "SQLITE_CONSTRAINT", 19): "sqlite_constraint",
}
_STORAGE_OS_REASONS = {
    errno.EACCES: "permission_denied", errno.EPERM: "permission_denied",
    errno.ENOENT: "path_unavailable", errno.ENOTDIR: "path_unavailable",
    errno.ENOSPC: "storage_full", errno.EIO: "storage_io",
}
_STORAGE_REASONS = frozenset({*_STORAGE_SQLITE_REASONS.values(), *_STORAGE_OS_REASONS.values(), "unknown"})
_STORAGE_OPERATIONS = frozenset({"open", "reconcile", "claim", "lease_lookup", "process", "finalize"})
_PRECLAIM_OPERATIONS = frozenset({"open", "reconcile", "claim", "lease_lookup"})


def storage_error_metadata(error: BaseException) -> dict[str, Any]:
    """Extract numeric OS/SQLite evidence only; exception text stays private."""
    sqlite_code = getattr(error, "sqlite_errorcode", None)
    if isinstance(sqlite_code, bool) or not isinstance(sqlite_code, int) or not 0 < sqlite_code <= 0x7fffffff:
        sqlite_code = None
    os_code = error.errno if isinstance(error, OSError) else getattr(error, "os_errno", None)
    if isinstance(os_code, bool) or not isinstance(os_code, int) or os_code not in errno.errorcode:
        os_code = None
    reason = (_STORAGE_SQLITE_REASONS.get(sqlite_code & 255, "unknown") if sqlite_code is not None
              else _STORAGE_OS_REASONS.get(os_code, "unknown"))
    return {"reason_code": reason, "sqlite_errorcode": sqlite_code, "os_errno": os_code}


def storage_failure_detail(error: BaseException, operation: str, *, preclaim: bool) -> dict[str, Any]:
    metadata = storage_error_metadata(error)
    return {"stage": "storage", "code": "storage_failure", "operation": operation, **metadata,
            "retry_safe": bool(preclaim and operation in _PRECLAIM_OPERATIONS
                               and getattr(error, "retry_safe", False) is True
                               and metadata["reason_code"] in {"sqlite_busy", "sqlite_locked"})}


def safe_storage_failure_detail(value: object) -> dict[str, Any] | None:
    """Validate persisted metadata without widening a retry permission."""
    if (not isinstance(value, Mapping) or value.get("stage") != "storage"
            or value.get("code") != "storage_failure" or not isinstance(value.get("operation"), str)
            or value["operation"] not in _STORAGE_OPERATIONS):
        return None
    sqlite_code, os_code = value.get("sqlite_errorcode"), value.get("os_errno")
    if isinstance(sqlite_code, bool) or not isinstance(sqlite_code, int) or not 0 < sqlite_code <= 0x7fffffff:
        sqlite_code = None
    if isinstance(os_code, bool) or not isinstance(os_code, int) or os_code not in errno.errorcode:
        os_code = None
    reason = value.get("reason_code")
    reason = reason if isinstance(reason, str) and reason in _STORAGE_REASONS else "unknown"
    expected = _STORAGE_SQLITE_REASONS.get(sqlite_code & 255, "unknown") if sqlite_code is not None else "unknown"
    return {"stage": "storage", "code": "storage_failure", "operation": value["operation"],
            "reason_code": reason, "sqlite_errorcode": sqlite_code, "os_errno": os_code,
            "retry_safe": bool(value.get("retry_safe") is True and value["operation"] in _PRECLAIM_OPERATIONS
                               and reason in {"sqlite_busy", "sqlite_locked"} and reason == expected)}


def record_failure_receipt(
    connection: sqlite3.Connection, job_id: str, attempt_count: int,
    code: str, reason_code: str | None, timestamp: str,
) -> None:
    """Store one immutable reason in the same transaction as the failed job."""
    reason = safe_failure_reason(code, reason_code)
    safe_code = code if isinstance(code, str) and code in _FAILURE_CODES else None
    connection.execute(
        "CREATE TABLE IF NOT EXISTS observation_failure_receipts ("
        "job_id TEXT NOT NULL REFERENCES observation_jobs(id) ON DELETE CASCADE,"
        "attempt_count INTEGER NOT NULL, error_code TEXT, reason_code TEXT, created_at TEXT NOT NULL,"
        "PRIMARY KEY(job_id, attempt_count))"
    )
    connection.execute(
        "INSERT OR IGNORE INTO observation_failure_receipts(job_id,attempt_count,error_code,reason_code,created_at) "
        "VALUES (?,?,?,?,?)", (job_id, attempt_count, safe_code, reason, timestamp),
    )


def read_failure_receipts(connection: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    """Legacy jobs have no receipt; absence is unknown, never reconstructed."""
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observation_failure_receipts'"
    ).fetchone() is None:
        return []
    rows = connection.execute(
        "SELECT attempt_count,error_code,reason_code,created_at FROM observation_failure_receipts "
        "WHERE job_id=? ORDER BY attempt_count DESC LIMIT 20", (job_id,),
    ).fetchall()
    return [{"attempt_count": row[0],
             "error_code": row[1] if row[1] in _FAILURE_CODES else None,
             "reason_code": safe_failure_reason(row[1], row[2]),
             "created_at": row[3]} for row in reversed(rows)]


def safe_failure_reason(code: object, reason: object) -> str | None:
    allowed = (INVALID_RESPONSE_REASONS if code == "invalid_response" else
               RUNNER_FAILURE_REASONS if code == "runner_failure" else frozenset())
    return reason if isinstance(reason, str) and reason in allowed else None
