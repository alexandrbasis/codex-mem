"""Content-free, per-attempt failure receipts for observation maintenance."""

import sqlite3
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
    "invalid_summary_text", "invalid_summary_metadata", "invalid_text", "invalid_tags",
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
