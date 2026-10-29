"""Bounded per-attempt metrics for the observation processor.

The ledger contains operational metadata only. Each claim stores its requested
profile before dispatch. Verified runtime identity and measured usage belong to
that attempt. Project and processor remain owned by ``observation_jobs``; model
input and output cannot enter this table through this API.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
import json
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
    existing = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='observer_usage_attempts'").fetchone()
    migrate = existing is not None and "requested_model" not in existing[0]
    if migrate:
        connection.execute("ALTER TABLE observer_usage_attempts RENAME TO observer_usage_attempts_legacy")
        connection.execute("DROP INDEX IF EXISTS observer_usage_running")
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
                usage_source IN ('app_server_thread_total', 'pi_runtime_total')),
            usage_updates INTEGER CHECK(usage_updates IS NULL OR usage_updates >= 0),
            input_tokens INTEGER CHECK(input_tokens IS NULL OR input_tokens >= 0),
            cached_input_tokens INTEGER CHECK(cached_input_tokens IS NULL OR cached_input_tokens >= 0),
            cache_write_input_tokens INTEGER CHECK(cache_write_input_tokens IS NULL OR cache_write_input_tokens >= 0),
            output_tokens INTEGER CHECK(output_tokens IS NULL OR output_tokens >= 0),
            reasoning_output_tokens INTEGER CHECK(reasoning_output_tokens IS NULL OR reasoning_output_tokens >= 0),
            total_tokens INTEGER CHECK(total_tokens IS NULL OR total_tokens >= 0),
            runtime TEXT,
            provider TEXT,
            requested_model TEXT,
            reasoning_effort TEXT,
            timeout REAL,
            profile_basis TEXT,
            actual_runtime TEXT,
            actual_provider TEXT,
            actual_model TEXT,
            actual_reasoning_effort TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            PRIMARY KEY(job_id, attempt_count)
        )
        """
    )
    if migrate:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(observer_usage_attempts_legacy)")]
        names = ",".join(columns)
        connection.execute(f"INSERT INTO observer_usage_attempts ({names}) SELECT {names} FROM observer_usage_attempts_legacy")
        connection.execute("DROP TABLE observer_usage_attempts_legacy")
        # Old job fields never change. Backfill requested identity only: no
        # receipt proves the actual model of a historical failed attempt.
        connection.execute("""
            UPDATE observer_usage_attempts SET runtime='codex', provider='openai',
                requested_model=(SELECT model FROM observation_jobs WHERE id=job_id),
                reasoning_effort=(SELECT reasoning_effort FROM observation_jobs WHERE id=job_id),
                profile_basis='legacy_requested_job_profile'
        """)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(observer_usage_attempts)")}
    if "execution_receipt" not in columns:
        connection.execute("ALTER TABLE observer_usage_attempts ADD COLUMN execution_receipt TEXT")
    if "execution_calls" not in columns:
        connection.execute("ALTER TABLE observer_usage_attempts ADD COLUMN execution_calls TEXT")
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
    _recover_call_usage(connection, checked_job_id, checked_attempt)
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
                _recover_call_usage(connection, row["job_id"], row["attempt_count"])
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
        "SELECT id, project, attempt_count, model, reasoning_effort FROM observation_jobs WHERE id = ? AND project = ?",
        (job_id, workspace),
    ).fetchone()


def begin_attempt(store: Store, project: str | Path, job_id: str, attempt_count: int, *, profile=None) -> None:
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
            begin_claim_attempt(connection, job, profile=profile)

        store._write(operation)


def begin_claim_attempt(connection: sqlite3.Connection, job: Mapping[str, Any], *, profile=None) -> None:
    """Snapshot a claimed profile inside the owning lease transaction."""
    _install_schema(connection)
    now = _utc_now()
    connection.execute(
        """
        INSERT OR IGNORE INTO observer_usage_attempts(
            job_id, attempt_count, outcome, started_at, runtime, provider,
            requested_model, reasoning_effort, timeout, profile_basis
        ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
        """,
        (job["id"], job["attempt_count"], now,
         profile.runtime if profile else "codex", profile.provider if profile else "openai",
         profile.model if profile else job["model"],
         profile.reasoning_effort if profile else job["reasoning_effort"],
         profile.timeout if profile else None,
         "requested_attempt_profile" if profile else "requested_job_profile"),
    )



def _normalize_metrics(metrics: Mapping[str, Any] | None) -> dict[str, Any]:
    result = {
        "duration_ms": None,
        "execution_receipt": None,
        "usage_status": "unavailable",
        "usage_source": None,
        "usage_updates": None,
        **{counter: None for counter in COUNTERS},
    }
    if metrics is None:
        return result
    if not isinstance(metrics, Mapping) or set(metrics) - {"generations"} not in (
            {"duration_ms", "usage"}, {"duration_ms", "usage", "execution_receipt"}):
        raise ValueError("metrics must contain duration_ms and usage")
    from .execution import pi_execution_receipt
    evidence = pi_execution_receipt(metrics.get("execution_receipt"))
    if evidence is not None:
        result["execution_receipt"] = json.dumps(evidence, sort_keys=True)
    result["duration_ms"] = _integer(metrics["duration_ms"], "duration_ms")
    usage = metrics["usage"]
    if not isinstance(usage, Mapping) or set(usage) != {"status", "source", "updates", "tokens"}:
        raise ValueError("usage must contain status, source, updates, and tokens")
    status = usage["status"]
    if status not in USAGE_STATUSES:
        raise ValueError("invalid usage status")
    pi_usage = usage["source"] == "pi_assistant_usage"
    if usage["source"] not in {USAGE_SOURCE, "pi_runtime_total", "pi_assistant_usage"}:
        raise ValueError("invalid usage source")
    result["usage_status"] = status
    result["usage_source"] = "pi_runtime_total" if pi_usage else usage["source"]
    result["usage_updates"] = _integer(usage["updates"], "usage updates")
    tokens = usage["tokens"]
    if status in {"unavailable", "invalid"}:
        if tokens is not None:
            raise ValueError("unknown usage counters must be null")
        return result
    if not isinstance(tokens, Mapping) or (not pi_usage and set(tokens) != set(COUNTERS)) or set(tokens) - set(COUNTERS):
        raise ValueError("reported usage must contain every token counter")
    for counter in COUNTERS:
        if counter in tokens:
            result[counter] = _integer(tokens[counter], counter)
    if (
        (all(result[key] is not None for key in ("cached_input_tokens", "cache_write_input_tokens", "input_tokens"))
         and result["cached_input_tokens"] + result["cache_write_input_tokens"] > result["input_tokens"])
        or (result["reasoning_output_tokens"] is not None and result["output_tokens"] is not None
            and result["reasoning_output_tokens"] > result["output_tokens"])
        or (all(result[key] is not None for key in ("total_tokens", "input_tokens", "output_tokens"))
            and result["total_tokens"] != result["input_tokens"] + result["output_tokens"])
    ):
        raise ValueError("inconsistent usage counters")
    return result


def _merge(existing: sqlite3.Row, incoming: dict[str, Any], outcome: str) -> dict[str, Any]:
    terminal = existing["outcome"] != "running"
    merged_outcome = existing["outcome"] if terminal else outcome
    merged: dict[str, Any] = {"outcome": merged_outcome}
    fields = (
        "error_code",
        "execution_receipt",
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
        (incoming[counter] is None and (existing[counter] is None or incoming["usage_source"] == "pi_runtime_total"))
        or (incoming[counter] is not None
            and (existing[counter] is None or incoming[counter] >= existing[counter]))
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
        elif incoming["usage_source"] == "pi_runtime_total" and new_status in {"reported", "partial"}:
            # Pi can omit an optional counter in a later generation. A prior
            # value is not the cumulative value of that now-unknown field.
            for counter in COUNTERS:
                if incoming[counter] is None:
                    merged[counter] = None
    elif old_status == "reported":
        merged["usage_status"] = old_status
    elif old_status == "partial":
        # A reclaimed lease keeps its outcome, but a late final receipt from
        # that same worker can replace its incomplete cumulative snapshot.
        if new_status in {"reported", "partial"} and monotonic_counters and same_worker:
            merged["usage_status"] = new_status
            for field in (*COUNTERS, "duration_ms", "usage_updates"):
                if field in COUNTERS and incoming["usage_source"] == "pi_runtime_total":
                    # The late Pi sample is cumulative. A missing optional
                    # counter must remain unknown, including after recovery.
                    merged[field] = incoming[field]
                else:
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


def _generation_details(metrics):
    """Sanitize at most two diagnostic rows; these never contribute to totals."""
    rows = metrics.get("generations") if isinstance(metrics, Mapping) else None
    if rows is None:
        return None
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2:
        raise ValueError("invalid generation details")
    from .observation_diagnostics import safe_failure_reason
    result = []
    fields = {"number", "execution_id", "turn_id", "outcome", "error_code", "reason_code", "usage"}
    for number, row in enumerate(rows, 1):
        if (not isinstance(row, Mapping) or set(row) != fields or type(row["number"]) is not int
                or row["number"] != number or row["outcome"] not in ("success", "failed")):
            raise ValueError("invalid generation details")
        code = _safe_code(row["error_code"])
        if (row["outcome"] == "success") != (code is None):
            raise ValueError("invalid generation outcome")
        try:
            measurement = _normalize_metrics(dict(duration_ms=0, usage=row["usage"]))
        except ValueError:
            measurement = _normalize_metrics(None)
            measurement["usage_status"] = "invalid"
        measurement["duration_ms"] = None  # Per-generation duration was not measured.
        result.append(dict(number=number, execution_id=_identifier(row["execution_id"], "execution_id"),
            turn_id=_identifier(row["turn_id"], "turn_id"), outcome=row["outcome"], error_code=code,
            reason_code=safe_failure_reason(code, row["reason_code"]),
            metrics=measurement))
    return result


def record_execution_call(store, project, job_id, attempt_count, call_id, *, profile,
                          outcome="running", metrics=None, identity=None, failure=None,
                          worker_thread_id=None, worker_turn_id=None, review_diagnostic=None):
    """Persist bounded independent call receipts, replacing cumulative snapshots.

    The generator and each support review have separate identities and counters.
    Quotes, prompts, candidates and arbitrary error text cannot enter this record.
    """
    from .summary_outcome import MAX_OUTCOME_CHUNKS
    outcome_chunks = {f"summary_outcome_{attempt}_chunk_{chunk}"
                      for attempt in (1, 2) for chunk in range(1, MAX_OUTCOME_CHUNKS + 1)}
    if call_id not in outcome_chunks and call_id not in (
            "generation", "summary_support_1", "summary_support_2", "summary_source_1",
            "summary_outcome_1", "summary_outcome_2"):
        raise ValueError("invalid execution call")
    if outcome not in ("running", "success", "failed"):
        raise ValueError("invalid call outcome")
    from .execution import normalize_profile
    from .summary_support import safe_diagnostic
    checked_diagnostic = safe_diagnostic(review_diagnostic) if review_diagnostic is not None else None
    generations = _generation_details(metrics) if call_id == "generation" else None
    requested = normalize_profile(profile.to_dict())
    try:
        normalized = _normalize_metrics(metrics)
    except ValueError:
        normalized = _normalize_metrics(None)
        normalized['usage_status'] = 'invalid'
    actual = None
    if identity is not None:
        actual = {key: _identifier(getattr(identity, key), key, required=key != "reasoning_effort")
                  for key in ("runtime", "provider", "model", "reasoning_effort")}
        worker_thread_id, worker_turn_id = identity.execution_id, identity.turn_id
    code = _safe_code(failure.code) if failure is not None else None
    if failure is not None:
        worker_thread_id = failure.execution_id or worker_thread_id
        worker_turn_id = failure.turn_id or worker_turn_id
    incoming = dict(call_id=call_id, requested_profile=requested, actual_identity=actual,
                    outcome=outcome, error_code=code, metrics=normalized,
                    execution_id=_identifier(worker_thread_id, "execution_id"),
                    turn_id=_identifier(worker_turn_id, "turn_id"))
    workspace = project_key(project)
    with store._lock:
        store._require_open()
        def operation():
            connection = store._connection
            if _job(connection, workspace, job_id) is None:
                raise ValueError("observation job unavailable")
            row = connection.execute("SELECT execution_calls FROM observer_usage_attempts WHERE job_id=? AND attempt_count=?",
                                     (job_id, attempt_count)).fetchone()
            if row is None:
                raise ValueError("attempt unavailable")
            calls = json.loads(row[0]) if row[0] else {}
            old = calls.get(call_id)
            if old:
                if old["requested_profile"] != requested:
                    raise ValueError("call profile changed")
                if old["outcome"] != "running":
                    if checked_diagnostic is not None:
                        old["review_diagnostic"] = checked_diagnostic
                        connection.execute("UPDATE observer_usage_attempts SET execution_calls=? WHERE job_id=? AND attempt_count=?",
                                           (json.dumps(calls, sort_keys=True), job_id, attempt_count))
                    return
                if old["actual_identity"] is not None:
                    if actual is not None and old["actual_identity"] != actual:
                        raise ValueError("call identity changed")
                    incoming["actual_identity"] = old["actual_identity"]
                for key in ("execution_id", "turn_id"):
                    incoming[key] = incoming[key] or old[key]
                if metrics is None or normalized["usage_status"] == "unavailable":
                    incoming["metrics"] = old["metrics"]
                    if incoming["metrics"]["usage_status"] == "reported" and outcome == "failed":
                        incoming["metrics"]["usage_status"] = "partial"
            if generations is not None or (old and old.get("generations")):
                previous = old.get("generations", []) if old else []
                if generations is not None and generations[:len(previous)] != previous:
                    raise ValueError("generation details changed")
                incoming["generations"] = generations if generations is not None else previous
            if checked_diagnostic is not None or (old and old.get("review_diagnostic")):
                incoming["review_diagnostic"] = checked_diagnostic or old["review_diagnostic"]
            calls[call_id] = incoming
            connection.execute("UPDATE observer_usage_attempts SET execution_calls=? WHERE job_id=? AND attempt_count=?",
                               (json.dumps(calls, sort_keys=True), job_id, attempt_count))
        store._write(operation)


def _aggregate_calls(calls, duration_ms, generation):
    """Sum independent measured calls once; preserve unknown counters."""
    rows = [call["metrics"] for call in calls.values()]
    result = dict(generation)
    result["duration_ms"] = duration_ms
    statuses = [row["usage_status"] for row in rows]
    result["usage_status"] = ("reported" if all(status == "reported" for status in statuses)
                              else "partial" if any(status in ("reported", "partial") for status in statuses)
                              else "unavailable")
    result["usage_updates"] = sum(row["usage_updates"] or 0 for row in rows)
    for key in COUNTERS:
        result[key] = sum(row[key] for row in rows) if all(row[key] is not None for row in rows) else None
    return result


def _saved_call_totals(existing, duration_ms):
    """Saved independent calls own totals once their ledger is present."""
    if not existing.get("execution_calls"):
        return None
    calls = json.loads(existing["execution_calls"])
    if not calls:
        return None
    aggregate = _aggregate_calls(calls, duration_ms, existing)
    if aggregate["usage_status"] == "reported" and any(call["outcome"] == "running" for call in calls.values()):
        aggregate["usage_status"] = "partial"
    return aggregate


def _recover_call_usage(connection, job_id, attempt_count):
    """Replace interrupted totals from saved calls inside the recovery transaction."""
    cursor = connection.execute(
        "SELECT * FROM observer_usage_attempts WHERE job_id=? AND attempt_count=? AND outcome='running'",
        (job_id, attempt_count))
    row = cursor.fetchone()
    if row is None:
        return
    existing = dict(zip((column[0] for column in cursor.description), row))
    aggregate = _saved_call_totals(existing, existing["duration_ms"])
    if aggregate is None:
        return
    # Do not infer whole-attempt elapsed time or overwrite generation provenance.
    fields = (*COUNTERS, "usage_status", "usage_updates")
    connection.execute(
        "UPDATE observer_usage_attempts SET " + ", ".join(f"{field}=?" for field in fields)
        + " WHERE job_id=? AND attempt_count=? AND outcome='running'",
        (*(aggregate[field] for field in fields), job_id, attempt_count))


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
    identity: Mapping[str, Any] | None = None,
    total_duration_ms: int | None = None,
) -> None:
    """Finish or enrich one attempt without replacing a known final snapshot."""

    if not isinstance(store, Store):
        raise TypeError("store must be a Store")
    workspace = project_key(project)
    checked_job_id = _identifier(job_id, "job_id", required=True)
    checked_attempt = _integer(attempt_count, "attempt_count", positive=True)
    if outcome not in OUTCOMES:
        raise ValueError("invalid observer outcome")
    actual = None
    if identity is not None:
        if set(identity) != {"runtime", "provider", "model", "reasoning_effort"}:
            raise ValueError("invalid execution identity")
        actual = {key: _identifier(value, key, required=key != "reasoning_effort")
                  for key, value in identity.items()}
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
            if actual is not None:
                # Identity is immutable once observed, including late receipts.
                keys = ("runtime", "provider", "model", "reasoning_effort")
                old = tuple(existing["actual_" + key] for key in keys)
                new = tuple(actual[key] for key in keys)
                if any(value is not None for value in old) and old != new:
                    raise ValueError("execution identity changed")
                connection.execute(
                    "UPDATE observer_usage_attempts SET actual_runtime=?, actual_provider=?, "
                    "actual_model=?, actual_reasoning_effort=? WHERE job_id=? AND attempt_count=?",
                    (*new, checked_job_id, checked_attempt))
            merged = _merge(existing, normalized, outcome)
            if outcome != "running":
                aggregate = _saved_call_totals(dict(existing), merged["duration_ms"])
                if aggregate is not None:
                    if total_duration_ms is not None and existing["outcome"] in ("running", "lease_expired"):
                        # Only an explicitly measured whole-attempt duration may replace it.
                        aggregate["duration_ms"] = _integer(total_duration_ms, "duration_ms")
                    # A late generator receipt cannot erase independent reviewer gaps.
                    for key in (*COUNTERS, "usage_status", "usage_updates", "duration_ms"):
                        merged[key] = aggregate[key]
            terminal_at = existing["finished_at"]
            if merged["outcome"] != "running" and terminal_at is None:
                terminal_at = _utc_now()
            assignments = (
                "outcome = ?, error_code = ?, worker_thread_id = ?, worker_turn_id = ?, "
                "duration_ms = ?, usage_status = ?, usage_source = ?, usage_updates = ?, "
                + ", ".join(f"{counter} = ?" for counter in COUNTERS)
                + ", finished_at = ?, execution_receipt = ?"
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
                    merged["execution_receipt"],
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
    identity: Mapping[str, Any] | None = None,
) -> None:
    """Persist one cumulative progress snapshot without finalizing the attempt.

    Callers throttle these writes; snapshots replace totals, never add them.
    A later recovery keeps their counters as partial unless native completion
    had already been observed. No worker prompt or output is accepted here.
    """

    finish_attempt(
        store, project, job_id, attempt_count, outcome="running",
        worker_thread_id=worker_thread_id, worker_turn_id=worker_turn_id,
        metrics=metrics, identity=identity,
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


def attempt_model_metadata(attempt: Mapping[str, Any], job: Mapping[str, Any]) -> dict[str, Any]:
    """Choose observed identity when present and label requested-only evidence."""
    actual = attempt.get("actual_model")
    return {
        "runtime": attempt.get("actual_runtime") or attempt.get("runtime") or attempt.get("requested_runtime") or "codex",
        "provider": attempt.get("actual_provider") or attempt.get("provider") or attempt.get("requested_provider") or "openai",
        "model": actual or attempt.get("requested_model") or job.get("model"),
        "model_source": "runtime_verified" if actual else attempt.get("profile_basis") or "requested_job_profile",
        "reasoning_effort": (attempt.get("actual_reasoning_effort") if actual
                             else attempt.get("requested_reasoning_effort", attempt.get("reasoning_effort", job.get("reasoning_effort")))),
    }


def _profile_model_rows(connection, workspace, aggregate_parts):
    """Aggregate attempt profiles and retain missing historical receipts as gaps."""
    columns = [row[1] for row in connection.execute("PRAGMA table_info(observer_usage_attempts)")]
    if "requested_model" not in columns:
        return None
    gap_values = {"job_id": "j.id", "runtime": "'codex'", "provider": "'openai'",
                  "requested_model": "j.model", "reasoning_effort": "j.reasoning_effort",
                  "profile_basis": "'requested_job_profile'"}
    gap_columns = ",".join(gap_values.get(column, "NULL") for column in columns)
    parts = [part.replace("COUNT(attempts.job_id)", "COUNT(attempts.attempt_count)")
             .replace("attempts.job_id IS NOT NULL", "attempts.attempt_count IS NOT NULL")
             for part in aggregate_parts]
    parts[1] = "COUNT(attempts.attempt_count) + SUM(attempts.missing) AS attempt_count"
    expressions = {
        "runtime": "COALESCE(attempts.actual_runtime,attempts.runtime,'codex')",
        "provider": "COALESCE(attempts.actual_provider,attempts.provider,'openai')",
        "model": "COALESCE(attempts.actual_model,attempts.requested_model)",
        "reasoning_effort": "CASE WHEN attempts.actual_model IS NOT NULL THEN attempts.actual_reasoning_effort ELSE attempts.reasoning_effort END",
    }
    dimensions = "jobs.processor_id," + ",".join(expressions.values())
    selected_dimensions = "jobs.processor_id," + ",".join(value + " AS " + key for key, value in expressions.items())
    basis = "CASE WHEN attempts.actual_model IS NOT NULL THEN 'runtime_verified' ELSE attempts.profile_basis END"
    sql = ("WITH profile_attempts AS (SELECT a.*,0 AS missing FROM observer_usage_attempts a "
           "JOIN observation_jobs j ON j.id=a.job_id WHERE j.project=? UNION ALL SELECT " + gap_columns +
           ",j.attempt_count-COALESCE(n.receipts,0) AS missing FROM observation_jobs j "
           "LEFT JOIN (SELECT job_id,COUNT(*) receipts FROM observer_usage_attempts GROUP BY job_id) n ON n.job_id=j.id "
           "WHERE j.project=? AND j.attempt_count>COALESCE(n.receipts,0)) SELECT " + selected_dimensions +
           ",CASE WHEN MIN(" + basis + ")=MAX(" + basis + ") THEN MIN(" + basis + ") "
           "ELSE 'mixed_attempt_evidence' END AS model_basis,GROUP_CONCAT(attempts.error_code) AS error_codes," + ",".join(parts) +
           " FROM observation_jobs jobs JOIN profile_attempts attempts ON attempts.job_id=jobs.id "
           "WHERE jobs.project=? GROUP BY " + dimensions + " ORDER BY " + dimensions)
    cursor = connection.execute(sql, (workspace, workspace, workspace))
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


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
    model_rows = _profile_model_rows(connection, workspace, aggregate_parts)
    for row in model_rows if model_rows is not None else rows:
        model_group = _group([row])
        result["models"].append(
            {
                "processor_id": row["processor_id"],
                "model": row["model"],
                "model_basis": row.get("model_basis", "requested_job_profile"),
                "error_code_counts": dict(Counter(filter(None, (row.get("error_codes") or "").split(",")))),
                "runtime": row.get("runtime", "codex"), "provider": row.get("provider", "openai"),
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
