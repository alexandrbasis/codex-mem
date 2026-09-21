"""Project-scoped exact-input cache of validated Jev judgments, without source text."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jev_filter import MODEL, POLICY_VERSION, QUESTIONS, JevFilterError, _validate
from .store import Store, project_key

MAX_PROJECT_ENTRIES = 10_000


def payload_key(payload: Mapping[str, Any], policy_version: str = POLICY_VERSION) -> str:
    """Hash every input and the explicit policy version using canonical JSON."""
    if not isinstance(payload, Mapping) or not isinstance(policy_version, str) or not policy_version:
        raise ValueError("invalid Jev cache input")
    try:
        encoded = json.dumps({"policy_version": policy_version, "payload": dict(payload)},
                             sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, OverflowError):
        raise ValueError("invalid Jev cache input") from None
    return hashlib.sha256(encoded).hexdigest()


def _supported_payload(payload: Mapping[str, Any]) -> bool:
    return (isinstance(payload, Mapping) and payload.get("model") == MODEL
            and isinstance(payload.get("questions"), Mapping)
            and set(payload["questions"]) == set(QUESTIONS))


def _safe_response(response: Any) -> dict[str, Any]:
    decision, _usage = _validate(response)
    # Rebuild rather than copy: even unknown nested API fields may contain text.
    return {"model": MODEL, "answers": {
        "useful": {"type": "noul", "noul": decision["useful_probability"]},
        "category": {"type": "choice", "choice": decision["category"],
                     "confidence": decision["confidence"],
                     "probabilities": decision["probabilities"]}},
        "usage": {"input_tokens": 0, "output_tokens": 0}}


def cache_get(store: Store, project: str | Path, payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return only a fully revalidated cached answer; corrupt or older rows miss."""
    if not _supported_payload(payload):
        return None
    key = payload_key(payload)
    workspace = project_key(project)
    with store._lock:
        store._require_open()
        connection = store._connection
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jev_evaluation_cache'").fetchone() is None:
            return None
        row = connection.execute("SELECT response_json FROM jev_evaluation_cache WHERE project=? AND cache_key=?",
                                 (workspace, key)).fetchone()
        if row is None:
            return None
        try:
            return _safe_response(json.loads(row[0]))
        except (JevFilterError, ValueError, TypeError, OverflowError, RecursionError):
            return None


def cache_put(store: Store, project: str | Path, payload: Mapping[str, Any], response: Mapping[str, Any]) -> None:
    """Persist a successful typed answer and prune oldest project-local entries."""
    if not _supported_payload(payload):
        raise JevFilterError("jev_filter_invalid_input")
    safe = _safe_response(response)
    key = payload_key(payload)
    workspace = project_key(project)
    serialized = json.dumps(safe, sort_keys=True, allow_nan=False, separators=(",", ":"))
    with store._lock:
        store._require_open()
        def operation() -> None:
            connection = store._connection
            connection.execute("""CREATE TABLE IF NOT EXISTS jev_evaluation_cache (
                project TEXT NOT NULL, cache_key TEXT NOT NULL,
                response_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(project, cache_key))""")
            connection.execute("""INSERT INTO jev_evaluation_cache VALUES (?, ?, ?, ?)
                ON CONFLICT(project, cache_key) DO UPDATE SET response_json=excluded.response_json""",
                (workspace, key, serialized, datetime.now(timezone.utc).isoformat()))
            connection.execute("""DELETE FROM jev_evaluation_cache WHERE project=? AND cache_key IN (
                SELECT cache_key FROM jev_evaluation_cache WHERE project=?
                ORDER BY created_at DESC, cache_key DESC LIMIT -1 OFFSET ?)""",
                (workspace, workspace, MAX_PROJECT_ENTRIES))
        store._write(operation)
