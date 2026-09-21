"""Bounded per-attempt metrics for the observation processor.

The ledger contains operational metadata only. Project, processor, model, and
reasoning effort remain owned by ``observation_jobs`` and are joined at read
time, so model input and output can never enter this table through this API.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import Store, project_key


COUNTERS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
OUTCOMES = ("running", "processed", "skipped", "failed", "lease_expired")
USAGE_STATUSES = ("reported", "partial", "unavailable", "invalid")
USAGE_SOURCE = "app_server_thread_total"
MAX_INTEGER = 2**63 - 1
ERROR_CODES = {
    "invalid_request",
    "invalid_response",
    "model_mismatch",
    "model_unavailable",
    "protocol_error",
    "rerouted",
    "runner_failure",
    "runner_unavailable",
    "storage_failure",
    "lease_expired",
    "timeout",
    "tool_called",
    "tools_available",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= MAX_INTEGER:
        raise ValueError(f"{field} must be an integer between {minimum} and {MAX_INTEGER}")
    return value


def _identifier(value: Any, field: str, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"invalid {field}")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"invalid {field}")
    return value


def _safe_code(value: Any) -> str | None:
    if value is None:
        return None
    if value not in ERROR_CODES:
        raise ValueError("invalid error_code")
    return value


def _install_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS observer_usage_attempts (
            job_id TEXT NOT NULL REFERENCES observation_jobs(id) ON DELETE CASCADE,
            attempt_count INTEGER NOT NULL CHECK(attempt_count > 0),
            outcome TEXT NOT NULL CHECK(outcome IN
                ('running', 'processed', 'skipped', 'failed', 'lease_expired')),
            error_code TEXT,
            worker_thread_id TEXT,
            worker_turn_id TEXT,
            duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0),
            usage_status TEXT CHECK(usage_status IS NULL OR usage_status IN
                ('reported', 'partial', 'unavailable', 'invalid')),
            usage_source TEXT CHECK(usage_source IS NULL OR
                usage_source = 'app_server_thread_total'),
            usage_updates INTEGER CHECK(usage_updates IS NULL OR usage_updates >= 0),
            input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens >= 0),
            cached_input_tokens INTEGER CHECK(cached_input_tokens IS NULL OR cached_input_tokens >= 0),
            cache_write_input_tokens INTEGER CHECK(cache_write_input_tokens IS NULL OR cache_write_input_tokens >= 0),
            output_tokens INTEGER CHECK(output_tokens IS NULL OR output_tokens >= 0),
            reasoning_output_tokens INTEGER CHECK(reasoning_output_tokens IS NULL OR reasoning_output_tokens >= 0),
            total_tokens INTEGER CHECK(total_tokens IS NULL OR total_tokens >= 0),
            started_at TEXT NOT NULL,
            finished_at TEXT,
            PRIMARY KEY(job_id, attempt_count)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS observer_usage_running "
        "ON observer_usage_attempts(started_at, job_id) WHERE outcome = 'running'"
    )


def recover_attempt(
    connection: sqlite3.Connection,
    job_id: str,
    attempt_count: int,
    *,
    outcome: str,
    error_code: str | None = None,
) -> None:
    """Finalize a prior running receipt inside the caller's claim transaction.

    The caller must invoke this while the prior observation job state is still
    available. Older databases without the optional ledger remain unchanged.
    """

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection")
    checked_job_id = _identifier(job_id, "job_id", required=True)
    checked_attempt = _integer(attempt_count, "attempt_count", positive=True)
    if outcome not in {"failed", "lease_expired"}:
        raise ValueError("recovered outcome must be failed or lease_expired")
    if outcome == "lease_expired":
        checked_code = "lease_expired"
    else:
        checked_code = error_code if error_code in ERROR_CODES else "runner_failure"
    present = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'observer_usage_attempts'"
    ).fetchone()
    if present is None:
        return
    connection.execute(
        """
        UPDATE observer_usage_attempts
        SET outcome = ?, error_code = ?, finished_at = COALESCE(finished_at, ?),
            usage_status = COALESCE(usage_status, 'unavailable')
        WHERE job_id = ? AND attempt_count = ? AND outcome = 'running'
        """,
        (outcome, checked_code, _utc_now(), checked_job_id, checked_attempt),
    )


def reconcile_attempts(
    store: Store, project: str | Path | None = None, *, limit: int = 128,
) -> int:
    """Recover a bounded set of receipts after an interrupted terminal write.

    Persisted job outcomes and expired leases are the only recovery evidence.
    Active leases remain untouched, and completed jobs do not turn partial
    counters into reported totals. This does not change observation job state.
    """

    if not isinstance(store, Store):
        raise TypeError("store must be a Store")
    checked_limit = _integer(limit, "limit", positive=True)
    if checked_limit > 1000:
        raise ValueError("limit must be at most 1000")
    workspace = project_key(project) if project is not None else None
    with store._lock:
        store._require_open()
        connection = store._connection
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observer_usage_attempts'"
        ).fetchone() is None:
            return 0

        def operation() -> int:
            now = _utc_now()
            rows = connection.execute(
                """
                SELECT attempts.job_id, attempts.attempt_count, jobs.status,
                       jobs.error_code, jobs.worker_thread_id, jobs.worker_turn_id,
                       jobs.updated_at, jobs.completed_at
                FROM observer_usage_attempts AS attempts
                JOIN observation_jobs AS jobs ON jobs.id = attempts.job_id
                WHERE attempts.outcome = 'running'
                  AND attempts.attempt_count = jobs.attempt_count
                  AND (? IS NULL OR jobs.project = ?)
                  AND (jobs.status IN ('processed', 'skipped', 'failed')
                       OR (jobs.status = 'running' AND jobs.lease_expires_at <= ?))
                ORDER BY attempts.started_at, attempts.job_id LIMIT ?
                """, (workspace, workspace, now, checked_limit),
            ).fetchall()
            for row in rows:
                expired = row["status"] == "running"
                outcome = "lease_expired" if expired else row["status"]
                code = None
                if expired:
                    code = "lease_expired"
                elif outcome == "failed":
                    code = row["error_code"] if row["error_code"] in ERROR_CODES else "runner_failure"
                connection.execute(
                    """
                    UPDATE observer_usage_attempts
                    SET outcome = ?, error_code = ?,
                        usage_status = COALESCE(usage_status, 'unavailable'),
                        worker_thread_id = COALESCE(worker_thread_id, ?),
                        worker_turn_id = COALESCE(worker_turn_id, ?),
                        finished_at = COALESCE(finished_at, ?)
                    WHERE job_id = ? AND attempt_count = ? AND outcome = 'running'
                    """,
                    (outcome, code, row["worker_thread_id"], row["worker_turn_id"],
                     now if expired else row["completed_at"] or row["updated_at"],
                     row["job_id"], row["attempt_count"]),
                )
            return len(rows)

        return store._write(operation)


def _job(
    connection: sqlite3.Connection, workspace: str, job_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT id, project, attempt_count FROM observation_jobs WHERE id = ? AND project = ?",
        (job_id, workspace),
    ).fetchone()


def begin_attempt(store: Store, project: str | Path, job_id: str, attempt_count: int) -> None:
    """Create the receipt for the currently claimed attempt before model work."""

    if not isinstance(store, Store):
        raise TypeError("store must be a Store")
    workspace = project_key(project)
    checked_job_id = _identifier(job_id, "job_id", required=True)
    checked_attempt = _integer(attempt_count, "attempt_count", positive=True)

    with store._lock:
        store._require_open()

        def operation() -> None:
            connection = store._connection
            job = _job(connection, workspace, checked_job_id)
            if job is None:
                raise ValueError("observation job is unavailable")
            if job["attempt_count"] != checked_attempt:
                raise ValueError("observation attempt is stale")
            _install_schema(connection)
            now = _utc_now()
            connection.execute(
                """
                INSERT OR IGNORE INTO observer_usage_attempts(
                    job_id, attempt_count, outcome, started_at
                ) VALUES (?, ?, 'running', ?)
                """,
                (checked_job_id, checked_attempt, now),
            )

        store._write(operation)


def _normalize_metrics(metrics: Mapping[str, Any] | None) -> dict[str, Any]:
    result = {
        "duration_ms": None,
        "usage_status": "unavailable",
        "usage_source": None,
        "usage_updates": None,
        **{counter: None for counter in COUNTERS},
    }
    if metrics is None:
        return result
    if not isinstance(metrics, Mapping) or set(metrics) != {"duration_ms", "usage"}:
        raise ValueError("metrics must contain duration_ms and usage")
    result["duration_ms"] = _integer(metrics["duration_ms"], "duration_ms")
    usage = metrics["usage"]
    if not isinstance(usage, Mapping) or set(usage) != {"status", "source", "updates", "tokens"}:
        raise ValueError("usage must contain status, source, updates, and tokens")
    status = usage["status"]
    if status not in USAGE_STATUSES:
        raise ValueError("invalid usage status")
    if usage["source"] != USAGE_SOURCE:
        raise ValueError("invalid usage source")
    result["usage_status"] = status
    result["usage_source"] = USAGE_SOURCE
    result["usage_updates"] = _integer(usage["updates"], "usage updates")
    tokens = usage["tokens"]
    if status in {"unavailable", "invalid"}:
        if tokens is not None:
            raise ValueError("unknown usage counters must be null")
        return result
    if not isinstance(tokens, Mapping) or set(tokens) != set(COUNTERS):
        raise ValueError("reported usage must contain every token counter")
    for counter in COUNTERS:
        result[counter] = _integer(tokens[counter], counter)
    if (
        result["cached_input_tokens"] + result["cache_write_input_tokens"]
        > result["input_tokens"]
        or result["reasoning_output_tokens"] > result["output_tokens"]
        or result["total_tokens"]
        != result["input_tokens"] + result["output_tokens"]
    ):
        raise ValueError("inconsistent usage counters")
    return result


def _merge(existing: sqlite3.Row, incoming: dict[str, Any], outcome: str) -> dict[str, Any]:
    terminal = existing["outcome"] != "running"
    merged_outcome = existing["outcome"] if terminal else outcome
    merged: dict[str, Any] = {"outcome": merged_outcome}
    fields = (
        "error_code",
        "worker_thread_id",
        "worker_turn_id",
        "duration_ms",
        "usage_source",
        "usage_updates",
        *COUNTERS,
    )
    for field in fields:
        if terminal:
            merged[field] = existing[field] if existing[field] is not None else incoming[field]
        else:
            merged[field] = incoming[field] if incoming[field] is not None else existing[field]
    for field in ("duration_ms", "usage_updates"):
        if not terminal and existing[field] is not None and incoming[field] is not None:
            merged[field] = max(existing[field], incoming[field])

    old_status = existing["usage_status"]
    new_status = incoming["usage_status"]
    monotonic_counters = all(
        incoming[counter] is not None
        and (existing[counter] is None or incoming[counter] >= existing[counter])
        for counter in COUNTERS
    )
    same_worker = all(
        existing[field] is None or incoming[field] is None or existing[field] == incoming[field]
        for field in ("worker_thread_id", "worker_turn_id")
    )
    if not terminal and incoming["usage_source"] is not None:
        if new_status == "unavailable" and old_status in {"reported", "partial"}:
            # A timeout, worker abort, or lost final response is not evidence
            # that the already persisted cumulative snapshot cost nothing.
            merged["usage_status"] = old_status
        elif new_status in {"reported", "partial"} and old_status in {"reported", "partial"} and not monotonic_counters:
            merged["usage_status"] = "invalid"
        else:
            merged["usage_status"] = new_status
        if merged["usage_status"] in {"unavailable", "invalid"}:
            for counter in COUNTERS:
                merged[counter] = None
    elif old_status == "reported":
        merged["usage_status"] = old_status
    elif old_status == "partial":
        # A reclaimed lease keeps its outcome, but a late final receipt from
        # that same worker can replace its incomplete cumulative snapshot.
        if new_status in {"reported", "partial"} and monotonic_counters and same_worker:
            merged["usage_status"] = new_status
            for field in (*COUNTERS, "duration_ms", "usage_updates"):
                merged[field] = max(existing[field] or 0, incoming[field] or 0)
        else:
            merged["usage_status"] = old_status
    elif new_status in {"reported", "partial"} and any(
        merged[counter] is not None for counter in COUNTERS
    ):
        merged["usage_status"] = new_status
    else:
        merged["usage_status"] = old_status or new_status
    return merged


def finish_attempt(
    store: Store,
    project: str | Path,
    job_id: str,
    attempt_count: int,
    *,
    outcome: str,
    error_code: str | None = None,
    worker_thread_id: str | None = None,
    worker_turn_id: str | None = None,
    metrics: Mapping[str, Any] | None = None,
) -> None:
    """Finish or enrich one attempt without replacing a known final snapshot."""

    if not isinstance(store, Store):
        raise TypeError("store must be a Store")
    workspace = project_key(project)
    checked_job_id = _identifier(job_id, "job_id", required=True)
    checked_attempt = _integer(attempt_count, "attempt_count", positive=True)
    if outcome not in OUTCOMES:
        raise ValueError("invalid observer outcome")
    normalized = _normalize_metrics(metrics)
    normalized.update(
        error_code=_safe_code(error_code),
        worker_thread_id=_identifier(worker_thread_id, "worker_thread_id"),
        worker_turn_id=_identifier(worker_turn_id, "worker_turn_id"),
    )

    with store._lock:
        store._require_open()

        def operation() -> None:
            connection = store._connection
            job = _job(connection, workspace, checked_job_id)
            if job is None:
                raise ValueError("observation job is unavailable")
            existing = connection.execute(
                "SELECT * FROM observer_usage_attempts WHERE job_id = ? AND attempt_count = ?",
                (checked_job_id, checked_attempt),
            ).fetchone()
            if existing is None:
                raise ValueError("observer attempt was not begun")
            if checked_attempt > job["attempt_count"]:
                raise ValueError("observation attempt is unavailable")
            if outcome == "running" and existing["outcome"] != "running":
                # A superseded worker cannot keep mutating its recovered
                # receipt through intermediate progress callbacks.
                return
            merged = _merge(existing, normalized, outcome)
            terminal_at = existing["finished_at"]
            if merged["outcome"] != "running" and terminal_at is None:
                terminal_at = _utc_now()
            assignments = (
                "outcome = ?, error_code = ?, worker_thread_id = ?, worker_turn_id = ?, "
                "duration_ms = ?, usage_status = ?, usage_source = ?, usage_updates = ?, "
                + ", ".join(f"{counter} = ?" for counter in COUNTERS)
                + ", finished_at = ?"
            )
            connection.execute(
                f"UPDATE observer_usage_attempts SET {assignments} "
                "WHERE job_id = ? AND attempt_count = ?",
                (
                    merged["outcome"],
                    merged["error_code"],
                    merged["worker_thread_id"],
                    merged["worker_turn_id"],
                    merged["duration_ms"],
                    merged["usage_status"],
                    merged["usage_source"],
                    merged["usage_updates"],
                    *(merged[counter] for counter in COUNTERS),
                    terminal_at,
                    checked_job_id,
                    checked_attempt,
                ),
            )

        store._write(operation)


def snapshot_attempt(
    store: Store,
    project: str | Path,
    job_id: str,
    attempt_count: int,
    *,
    worker_thread_id: str | None = None,
    worker_turn_id: str | None = None,
    metrics: Mapping[str, Any] | None = None,
) -> None:
    """Persist one cumulative progress snapshot without finalizing the attempt.

    Callers throttle these writes; snapshots replace totals, never add them.
    A later recovery keeps their counters as partial unless native completion
    had already been observed. No worker prompt or output is accepted here.
    """

    finish_attempt(
        store, project, job_id, attempt_count, outcome="running",
        worker_thread_id=worker_thread_id, worker_turn_id=worker_turn_id,
        metrics=metrics,
    )


def _empty_summary(workspace: str, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "project": workspace,
        "scope": "project",
        "attempts": {
            "jobs": 0,
            "expected": 0,
            "recorded": 0,
            "without_receipt": 0,
            "reported": 0,
            "partial": 0,
            "unknown": 0,
            "outcomes": {outcome: 0 for outcome in OUTCOMES},
        },
        "duration_ms": {"count": 0, "total": None, "average": None, "max": None},
        "totals": {"reported": None, "partial": None},
        "models": [],
    }


def _group(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    expected = sum(row["attempt_count"] for row in rows)
    recorded = sum(row["recorded"] for row in rows)
    duration_count = sum(row["duration_count"] for row in rows)
    duration_total = sum(row["duration_total"] for row in rows)
    result: dict[str, Any] = {
        "jobs": sum(row["jobs"] for row in rows),
        "expected": expected,
        "recorded": recorded,
        "without_receipt": max(0, expected - recorded),
        "reported": sum(row["reported"] for row in rows),
        "partial": sum(row["partial"] for row in rows),
        "unknown": sum(row["unknown_usage"] for row in rows),
        "outcomes": {
            outcome: sum(row[f"outcome_{outcome}"] for row in rows) for outcome in OUTCOMES
        },
        "duration_ms": {
            "count": duration_count,
            "total": duration_total if duration_count else None,
            "average": duration_total / duration_count if duration_count else None,
            "max": max(
                (row["duration_max"] for row in rows if row["duration_max"] is not None),
                default=None,
            ),
        },
        "totals": {},
    }
    for status in ("reported", "partial"):
        count = result[status]
        result["totals"][status] = (
            {
                "attempts": count,
                **{
                    counter: (
                        sum(row[f"{status}_{counter}"] for row in rows)
                        if any(row[f"{status}_{counter}_count"] for row in rows)
                        else None
                    )
                    for counter in COUNTERS
                },
                "counter_attempts": {
                    counter: sum(row[f"{status}_{counter}_count"] for row in rows)
                    for counter in COUNTERS
                },
            }
            if count
            else None
        )
    return result


def _ratio(numerator: int | float, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _outcome_efficiency(
    connection: sqlite3.Connection, workspace: str, recorded: int, present: bool,
) -> dict[str, Any]:
    """Aggregate receipts once by outcome; absent counters never become zero."""

    result = {
        outcome: {
            "attempts": 0,
            "percent_of_recorded_attempts": _ratio(0, recorded),
            "usage_status": {"reported": 0, "partial": 0, "unknown": 0},
            "reported_percent_of_outcome_attempts": None,
            "duration_ms": {"count": 0, "total": None, "average": None, "max": None},
            "totals": {"reported": None, "partial": None},
        }
        for outcome in OUTCOMES
    }
    if not present:
        return result
    counters = ", ".join(
        f"SUM(a.{counter}) AS {counter}, COUNT(a.{counter}) AS {counter}_count"
        for counter in COUNTERS
    )
    cursor = connection.execute(
        "SELECT a.outcome, a.usage_status, COUNT(*) AS attempts, "
        "COUNT(a.duration_ms) AS duration_count, SUM(a.duration_ms) AS duration_total, "
        "MAX(a.duration_ms) AS duration_max, " + counters +
        " FROM observation_jobs j JOIN observer_usage_attempts a ON a.job_id = j.id "
        "WHERE j.project = ? GROUP BY a.outcome, a.usage_status", (workspace,),
    )
    names = [column[0] for column in cursor.description]
    for values in cursor:
        row = dict(zip(names, values))
        item = result[row["outcome"]]
        count = row["attempts"]
        item["attempts"] += count
        status = row["usage_status"]
        usage_key = status if status in {"reported", "partial"} else "unknown"
        item["usage_status"][usage_key] += count
        if status in {"reported", "partial"}:
            item["totals"][status] = {
                "attempts": count,
                **{counter: row[counter] for counter in COUNTERS},
                "counter_attempts": {
                    counter: row[f"{counter}_count"] for counter in COUNTERS
                },
            }
        duration = item["duration_ms"]
        if row["duration_count"]:
            duration["count"] += row["duration_count"]
            duration["total"] = (duration["total"] or 0) + row["duration_total"]
            duration["max"] = max(duration["max"] or 0, row["duration_max"])
    for item in result.values():
        item["percent_of_recorded_attempts"] = _ratio(100 * item["attempts"], recorded)
        item["reported_percent_of_outcome_attempts"] = _ratio(
            100 * item["usage_status"]["reported"], item["attempts"],
        )
        duration = item["duration_ms"]
        duration["average"] = _ratio(duration["total"] or 0, duration["count"])
    return result


def _output_efficiency(
    connection: sqlite3.Connection, workspace: str, present: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve persisted output metadata once, independently of attempt joins.

    Counts describe retained records, including superseded records, rather than
    useful or accurate knowledge. Per-output costs use only complete job-level
    evidence, including every retry, with the same output denominator.
    """

    cost_columns = (
        "receipts", "reported_totals", "total_tokens", "duration_count",
        "duration_total", "success_receipt",
    )
    cost_cte = ""
    cost_join = ""
    cost_select = ", ".join(f"0 AS {column}" for column in cost_columns)
    if present:
        cost_cte = """, costs AS (
            SELECT j.id, COUNT(CASE WHEN a.outcome != 'running' THEN a.job_id END) AS receipts,
                SUM(CASE WHEN a.usage_status = 'reported' AND a.total_tokens IS NOT NULL
                         THEN 1 ELSE 0 END) AS reported_totals,
                SUM(CASE WHEN a.usage_status = 'reported' THEN a.total_tokens END) AS total_tokens,
                COUNT(a.duration_ms) AS duration_count, SUM(a.duration_ms) AS duration_total,
                MAX(CASE WHEN a.attempt_count = j.attempt_count AND a.outcome = 'processed'
                         THEN 1 ELSE 0 END) AS success_receipt
            FROM jobs j LEFT JOIN observer_usage_attempts a ON a.job_id = j.id
            GROUP BY j.id
        )"""
        cost_join = " LEFT JOIN costs c ON c.id = j.id "
        cost_select = ", ".join(f"c.{column}" for column in cost_columns)
    cursor = connection.execute(
        """WITH jobs AS (
            SELECT id, project, processor_id, attempt_count,
                CASE WHEN json_valid(output_ids_json) THEN
                    CASE WHEN json_type(output_ids_json) = 'array' THEN output_ids_json END
                END AS output_manifest
            FROM observation_jobs WHERE project = ? AND status = 'processed'
        )""" + cost_cte +
        " SELECT j.id AS job_id, j.attempt_count, j.output_manifest IS NOT NULL AS manifest_valid, "
        + cost_select + ", o.type AS output_type, o.value AS output_id, "
        "e.id AS entry_id, e.kind AS entry_kind "
        "FROM jobs j " + cost_join +
        " LEFT JOIN json_each(COALESCE(j.output_manifest, '[]')) o "
        "LEFT JOIN entries e ON o.type = 'text' AND e.id = o.value "
        "AND e.project = j.project AND e.source = 'processor:' || j.processor_id "
        "AND e.kind IN ('note', 'session_summary')", (workspace,),
    )
    names = [column[0] for column in cursor.description]
    jobs: dict[str, dict[str, Any]] = {}
    notes: set[str] = set()
    summaries: set[str] = set()
    for values in cursor:
        row = dict(zip(names, values))
        job = jobs.setdefault(row["job_id"], {
            **{column: row[column] for column in cost_columns},
            "attempt_count": row["attempt_count"],
            "manifest_valid": bool(row["manifest_valid"]),
            "outputs": set(), "unresolved": set(),
        })
        if row["entry_id"] is not None:
            job["outputs"].add(row["entry_id"])
            (notes if row["entry_kind"] == "note" else summaries).add(row["entry_id"])
        elif row["output_type"] is not None:
            job["unresolved"].add((row["output_type"], row["output_id"]))

    outputs = {
        "basis": "retained_output_records_of_processed_project_jobs",
        "processed_jobs": len(jobs),
        "persisted_output_records": len(notes | summaries),
        "notes": len(notes),
        "session_summaries": len(summaries),
        "jobs_without_success_receipt": 0,
        "jobs_with_incomplete_output_metadata": 0,
        "unresolved_output_references": 0,
    }
    per_output: dict[str, Any] = {
        "basis": "complete_processed_jobs_including_all_retry_attempts",
        "tokens": {"basis": "all_attempt_total_tokens_reported", "jobs": 0,
                   "attempts": 0, "output_records": 0,
                   "excluded_processed_jobs": 0, "total_tokens": None,
                   "total_tokens_per_output_record": None},
        "duration_ms": {"basis": "recorded_durations_of_all_terminal_attempts", "jobs": 0,
                        "attempts": 0, "output_records": 0,
                        "excluded_processed_jobs": 0, "total": None,
                        "per_output_record": None},
    }
    matched_outputs: dict[str, set[str]] = {"tokens": set(), "duration_ms": set()}
    for job in jobs.values():
        outputs["jobs_without_success_receipt"] += not job["success_receipt"]
        complete_outputs = job["manifest_valid"] and job["outputs"] and not job["unresolved"]
        outputs["jobs_with_incomplete_output_metadata"] += not bool(complete_outputs)
        outputs["unresolved_output_references"] += len(job["unresolved"])
        complete_receipts = (
            job["attempt_count"] > 0 and job["success_receipt"]
            and job["receipts"] == job["attempt_count"]
        )
        for key, count, total in (
            ("tokens", "reported_totals", "total_tokens"),
            ("duration_ms", "duration_count", "duration_total"),
        ):
            item = per_output[key]
            if not (complete_outputs and complete_receipts and job[count] == job["attempt_count"]):
                item["excluded_processed_jobs"] += 1
                continue
            item["jobs"] += 1
            item["attempts"] += job["attempt_count"]
            total_key = "total_tokens" if key == "tokens" else "total"
            item[total_key] = (item[total_key] or 0) + job[total]
            matched_outputs[key].update(job["outputs"])
    for key, item in ((key, per_output[key]) for key in matched_outputs):
        item["output_records"] = len(matched_outputs[key])
        total_key = "total_tokens" if key == "tokens" else "total"
        ratio_key = "total_tokens_per_output_record" if key == "tokens" else "per_output_record"
        item[ratio_key] = _ratio(item[total_key] or 0, item["output_records"])
    return outputs, per_output


def _efficiency_summary(
    connection: sqlite3.Connection, workspace: str, attempts: Mapping[str, Any], present: bool,
) -> dict[str, Any]:
    outputs, per_output = _output_efficiency(connection, workspace, present)
    return {
        "net_savings": "not_measured",
        "output_quality": "not_measured",
        "duration_basis": "recorded_elapsed_time_may_exclude_unobserved_runtime",
        "coverage": {
            "expected_attempts": attempts["expected"],
            "recorded_attempts": attempts["recorded"],
            "attempts_without_receipt": attempts["without_receipt"],
            "reported_attempts": attempts["reported"],
            "partial_attempts": attempts["partial"],
            "unknown_usage_attempts": attempts["unknown"],
            "receipt_percent_of_expected_attempts": _ratio(
                100 * attempts["recorded"], attempts["expected"],
            ),
            "reported_percent_of_recorded_attempts": _ratio(
                100 * attempts["reported"], attempts["recorded"],
            ),
            "reported_percent_of_expected_attempts": _ratio(
                100 * attempts["reported"], attempts["expected"],
            ),
        },
        "outcomes": _outcome_efficiency(connection, workspace, attempts["recorded"], present),
        "outputs": outputs,
        "per_output": per_output,
    }


def observer_usage_summary(
    connection: sqlite3.Connection, project: str | Path
) -> dict[str, Any]:
    """Read project metrics without changing the optional table.

    Model groups describe the requested observation job profile. A successful
    native runner validates that pin separately; a failed attempt does not
    prove which model ran.
    """

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be a sqlite3.Connection")
    workspace = project_key(project)
    result = _empty_summary(workspace, "unavailable")
    present = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'observer_usage_attempts'"
    ).fetchone()
    if present is None:
        cursor = connection.execute(
            """
            SELECT processor_id, model, reasoning_effort, COUNT(*) AS jobs,
                   SUM(attempt_count) AS expected
            FROM observation_jobs WHERE project = ?
            GROUP BY processor_id, model, reasoning_effort
            ORDER BY processor_id, model, reasoning_effort
            """,
            (workspace,),
        )
        names = [column[0] for column in cursor.description]
        rows = [dict(zip(names, row)) for row in cursor.fetchall()]
        result["attempts"]["jobs"] = sum(row["jobs"] for row in rows)
        result["attempts"]["expected"] = sum(row["expected"] for row in rows)
        result["attempts"]["without_receipt"] = result["attempts"]["expected"]
        for row in rows:
            attempts = dict(result["attempts"])
            attempts.update(
                jobs=row["jobs"],
                expected=row["expected"],
                without_receipt=row["expected"],
                outcomes=dict(result["attempts"]["outcomes"]),
            )
            result["models"].append(
                {
                    "processor_id": row["processor_id"],
                    "model": row["model"],
                    "model_basis": "requested_job_profile",
                    "reasoning_effort": row["reasoning_effort"],
                    "attempts": attempts,
                    "duration_ms": dict(result["duration_ms"]),
                    "totals": dict(result["totals"]),
                }
            )
        result["efficiency"] = _efficiency_summary(
            connection, workspace, result["attempts"], False,
        )
        return result

    aggregate_parts = [
        "COUNT(DISTINCT jobs.id) AS jobs",
        "(SELECT COALESCE(SUM(expected.attempt_count), 0) "
        "FROM observation_jobs AS expected WHERE expected.project = jobs.project "
        "AND expected.processor_id = jobs.processor_id AND expected.model = jobs.model "
        "AND expected.reasoning_effort = jobs.reasoning_effort) AS attempt_count",
        "COUNT(attempts.job_id) AS recorded",
        "SUM(CASE WHEN attempts.usage_status = 'reported' THEN 1 ELSE 0 END) AS reported",
        "SUM(CASE WHEN attempts.usage_status = 'partial' THEN 1 ELSE 0 END) AS partial",
        "SUM(CASE WHEN attempts.job_id IS NOT NULL AND (attempts.usage_status IS NULL OR attempts.usage_status IN ('unavailable', 'invalid')) THEN 1 ELSE 0 END) AS unknown_usage",
        "COUNT(attempts.duration_ms) AS duration_count",
        "COALESCE(SUM(attempts.duration_ms), 0) AS duration_total",
        "MAX(attempts.duration_ms) AS duration_max",
    ]
    aggregate_parts.extend(
        f"SUM(CASE WHEN attempts.outcome = '{outcome}' THEN 1 ELSE 0 END) AS outcome_{outcome}"
        for outcome in OUTCOMES
    )
    for status in ("reported", "partial"):
        aggregate_parts.extend(
            f"COALESCE(SUM(CASE WHEN attempts.usage_status = '{status}' THEN attempts.{counter} ELSE 0 END), 0) AS {status}_{counter}"
            for counter in COUNTERS
        )
        aggregate_parts.extend(
            f"COUNT(CASE WHEN attempts.usage_status = '{status}' THEN attempts.{counter} END) AS {status}_{counter}_count"
            for counter in COUNTERS
        )
    sql = (
        "SELECT jobs.processor_id, jobs.model, jobs.reasoning_effort, "
        + ", ".join(aggregate_parts)
        + " FROM observation_jobs AS jobs LEFT JOIN observer_usage_attempts AS attempts "
        "ON attempts.job_id = jobs.id WHERE jobs.project = ? "
        "GROUP BY jobs.processor_id, jobs.model, jobs.reasoning_effort "
        "ORDER BY jobs.processor_id, jobs.model, jobs.reasoning_effort"
    )
    cursor = connection.execute(sql, (workspace,))
    names = [column[0] for column in cursor.description]
    rows = [dict(zip(names, row)) for row in cursor.fetchall()]
    result["status"] = "available"
    if not rows:
        result["efficiency"] = _efficiency_summary(
            connection, workspace, result["attempts"], True,
        )
        return result
    grouped = _group(rows)
    result["attempts"] = {
        key: grouped[key]
        for key in (
            "jobs",
            "expected",
            "recorded",
            "without_receipt",
            "reported",
            "partial",
            "unknown",
            "outcomes",
        )
    }
    result["duration_ms"] = grouped["duration_ms"]
    result["totals"] = grouped["totals"]
    for row in rows:
        model_group = _group([row])
        result["models"].append(
            {
                "processor_id": row["processor_id"],
                "model": row["model"],
                "model_basis": "requested_job_profile",
                "reasoning_effort": row["reasoning_effort"],
                "attempts": {
                    key: model_group[key]
                    for key in (
                        "jobs",
                        "expected",
                        "recorded",
                        "without_receipt",
                        "reported",
                        "partial",
                        "unknown",
                        "outcomes",
                    )
                },
                "duration_ms": model_group["duration_ms"],
                "totals": model_group["totals"],
            }
        )
    result["efficiency"] = _efficiency_summary(
        connection, workspace, result["attempts"], True,
    )
    return result


__all__ = (
    "begin_attempt",
    "finish_attempt",
    "observer_usage_summary",
    "recover_attempt",
    "reconcile_attempts",
    "snapshot_attempt",
)
