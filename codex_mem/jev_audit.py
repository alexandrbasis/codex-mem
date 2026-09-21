"""Content-free, durable receipts for the Jev eligibility gate."""
from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import ObservationLeaseExpired, Store, project_key

CATEGORIES = frozenset({"decision", "verified_finding", "open_work", "preference",
                        "supporting_context", "routine", "other"})
V3_CATEGORIES = frozenset({"decision", "verification_result", "problem", "open_work",
                           "preference", "supporting_context", "routine", "other"})
ERROR_CODES = frozenset({"jev_filter_failure", "jev_filter_credentials", "jev_filter_timeout",
                         "jev_filter_transport", "jev_filter_invalid_response", "jev_filter_invalid_input"})
MAX_INTEGER = 2**63 - 1


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_INTEGER:
        raise ValueError("invalid audit counter")
    return value


def _probability(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1 or not math.isfinite(value):
        raise ValueError("invalid audit probability")
    return float(value)


def _choice(value: Any, allowed: set | frozenset) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("invalid audit classification")
    return value


def _version(value: Any, pattern: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError("invalid audit version")
    return value


def _known_id(connection: sqlite3.Connection, value: Any, project: str, job_id: str,
              *, claimed: bool) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid audit source")
    sql = "SELECT 1 FROM entries WHERE id=? AND project=?"
    args = [value, project]
    if claimed:
        sql += " AND id IN (SELECT source_id FROM observation_job_sources WHERE job_id=?)"
        args.append(job_id)
    if connection.execute(sql, args).fetchone() is None:
        raise ValueError("audit source is unavailable")
    return value


def _sanitize(connection: sqlite3.Connection, project: str, job_id: str,
              audit: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(audit, Mapping):
        raise ValueError("invalid filter audit")
    status = _choice(audit.get("status", "success"), {"success", "failure"})
    code = audit.get("error_code")
    if code is not None:
        code = _choice(code, ERROR_CODES)
    if status == "success" and code is not None:
        raise ValueError("successful audit cannot have an error")
    started = audit.get("generator_started", False)
    if not isinstance(started, bool):
        raise ValueError("invalid generator status")
    incomplete = audit.get("incomplete", False)
    if not isinstance(incomplete, bool) or (incomplete and status != "failure"):
        raise ValueError("invalid audit completeness")
    usage_status = _choice(audit.get("usage_status", "partial" if status == "failure" else "reported"),
                           {"partial", "reported", "unavailable"})
    if status == "failure" and usage_status == "reported":
        raise ValueError("failed audit usage cannot be complete")
    result = {"status": status, "error_code": code, "generator_started": started,
              "incomplete": incomplete, "usage_status": usage_status}
    if "duration_ms" in audit:
        result["duration_ms"] = _integer(audit["duration_ms"])
    for field, pattern in (("model", r"jev-\d+(?:\.\d+){1,2}"),
                           ("policy_version", r"memory-eligibility-v\d+")):
        value = audit.get(field)
        if value is not None:
            result[field] = _version(value, pattern)
    categories = V3_CATEGORIES if result.get("policy_version") == "memory-eligibility-v3" else CATEGORIES
    if "history_skipped" in audit:
        if not isinstance(audit["history_skipped"], bool):
            raise ValueError("invalid history status")
        result["history_skipped"] = audit["history_skipped"]
    decisions = audit.get("decisions", [])
    if not isinstance(decisions, list) or len(decisions) > 10000:
        raise ValueError("invalid audit decisions")
    clean_decisions = []
    seen = set()
    for decision in decisions:
        if not isinstance(decision, Mapping):
            raise ValueError("invalid audit decision")
        location = _choice(decision.get("location"), {"sources", "context"})
        source_id = _known_id(connection, decision.get("source_id"), project, job_id,
                              claimed=location == "sources")
        if (location, source_id) in seen:
            raise ValueError("duplicate audit decision")
        seen.add((location, source_id))
        route = _choice(decision.get("route"), {"retain", "discard", "incomplete"} if incomplete else {"retain", "discard"})
        chunks = decision.get("chunks", [])
        if not isinstance(chunks, list) or len(chunks) > 10000:
            raise ValueError("invalid audit chunks")
        clean_chunks = []
        for chunk in chunks:
            if not isinstance(chunk, Mapping):
                raise ValueError("invalid audit chunk")
            probabilities = chunk.get("probabilities")
            if not isinstance(probabilities, Mapping) or set(probabilities) != categories:
                raise ValueError("invalid audit distribution")
            clean_probabilities = {key: _probability(probabilities[key]) for key in sorted(categories)}
            if not math.isclose(sum(clean_probabilities.values()), 1, abs_tol=len(categories) * 0.005 + 1e-9):
                raise ValueError("invalid audit distribution sum")
            clean_chunks.append({"category": _choice(chunk.get("category"), categories),
                                 "route": _choice(chunk.get("route"), {"retain", "discard"}),
                                 "useful_probability": _probability(chunk.get("useful_probability")),
                                 "confidence": _probability(chunk.get("confidence")),
                                 "probabilities": clean_probabilities})
            if "evaluation_source" in chunk:
                clean_chunks[-1]["evaluation_source"] = _choice(chunk["evaluation_source"], {"live", "cache"})
        clean_decisions.append({"source_id": source_id, "location": location,
                                "route": route, "chunks": clean_chunks})
    result["decisions"] = clean_decisions
    retained = sum(item["route"] == "retain" for item in clean_decisions)
    discarded = sum(item["route"] == "discard" for item in clean_decisions)
    result["counts"] = {"evaluated": retained + discarded, "retained": retained,
                        "discarded": discarded,
                        "chunks": sum(len(item["chunks"]) for item in clean_decisions)}
    counters = audit.get("counts", {})
    for field in ("requests", "cache_hits"):
        if field in counters:
            result["counts"][field] = _integer(counters[field])
    usage = audit.get("usage", {})
    if not isinstance(usage, Mapping):
        raise ValueError("invalid audit usage")
    result["usage"] = {field: _integer(usage[field]) for field in ("input_tokens", "output_tokens") if field in usage}
    lifecycle = audit.get("lifecycle_only_ids", [])
    if not isinstance(lifecycle, list):
        raise ValueError("invalid audit lifecycle IDs")
    result["lifecycle_only_ids"] = [_known_id(connection, value, project, job_id, claimed=True) for value in lifecycle]
    return result


def record_filter_attempt(store: Store, project: str | Path, job_id: str,
                          attempt_count: int, audit: Mapping[str, Any]) -> None:
    """Upsert one receipt while its exact claimed attempt is still running.

    Unknown keys are discarded at every level. Source text, exception messages,
    credentials, and arbitrary response metadata have no persistence path.
    """
    if not isinstance(store, Store):
        raise TypeError("store must be a Store")
    workspace = project_key(project)
    if _integer(attempt_count) == 0:
        raise ValueError("invalid audit attempt")
    with store._lock:
        store._require_open()
        def operation() -> None:
            connection = store._connection
            job = connection.execute("SELECT status, attempt_count, lease_expires_at FROM observation_jobs WHERE id=? AND project=?",
                                     (job_id, workspace)).fetchone()
            if job is None or job["status"] != "running" or job["attempt_count"] != attempt_count:
                raise ValueError("observation attempt is unavailable or stale")
            safe = _sanitize(connection, workspace, job_id, audit)
            if safe["generator_started"]:
                try:
                    expiry = datetime.fromisoformat(job["lease_expires_at"].replace("Z", "+00:00"))
                    expired = expiry <= datetime.now(timezone.utc)
                except (AttributeError, TypeError, ValueError):
                    expired = True
                if expired:
                    raise ObservationLeaseExpired("Observation job lease expired")
            connection.execute("""CREATE TABLE IF NOT EXISTS jev_filter_attempts (
                job_id TEXT NOT NULL REFERENCES observation_jobs(id) ON DELETE CASCADE,
                attempt_count INTEGER NOT NULL CHECK(attempt_count > 0),
                audit_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(job_id, attempt_count))""")
            connection.execute("""INSERT INTO jev_filter_attempts VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id, attempt_count) DO UPDATE SET
                audit_json=excluded.audit_json, updated_at=excluded.updated_at""",
                (job_id, attempt_count, json.dumps(safe, allow_nan=False, sort_keys=True),
                 datetime.now(timezone.utc).isoformat()))
        store._write(operation)


def read_filter_attempts(connection: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    """Read the latest 20 attempts in chronological order without installing schema."""
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jev_filter_attempts'").fetchone() is None:
        return []
    rows = connection.execute("SELECT attempt_count,audit_json,updated_at FROM jev_filter_attempts WHERE job_id=? ORDER BY attempt_count DESC LIMIT 20",
                              (job_id,)).fetchall()
    return [{"attempt_count": row[0], **json.loads(row[1]), "updated_at": row[2]} for row in reversed(rows)]
