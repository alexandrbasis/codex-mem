"""Metadata-only usage ledger, separate from searchable memory content.

Input includes cached input; output includes reasoning output. Subset counters
must never be added again when calculating future task costs.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import Store, project_key

COUNTERS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
SESSION_FIELDS = ("thread_id", "session_id", "parent_thread_id", "project", "agent_path", "agent_role", "agent_nickname", "thread_source", "model_provider", "started_at")
EVENT_FIELDS = ("event_key", "thread_id", "session_id", "turn_id", "root_turn_id", "response_id", "model", "model_source", "model_provider", "service_tier", "recorded_at", "source_kind", *COUNTERS, "quality")
_STATE_KEYS = set(SESSION_FIELDS + COUNTERS) | {"owner", "session", "models", "turn_models", "current_model", "current_turn_id", "legacy_totals", "native_seen", "model", "model_source", "service_tier", "turn_id", "root_turn_id", "turns", "legacy_total", "skip_line", "invalid_owner", "inherited", "root_explicit"}


def _text(value: Any, field: str, *, required: bool = False, limit: int = 512) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f"Invalid usage {field}")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise ValueError(f"Invalid usage {field}")
    return value


def _timestamp(value: Any, field: str) -> str | None:
    value = _text(value, field)
    if value is not None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
        except ValueError:
            raise ValueError(f"Invalid usage {field}") from None
    return value


def _state(value: Any, *, dynamic: bool = False, depth: int = 0) -> Any:
    if depth > 6:
        raise ValueError("Usage parser state too deep")
    if isinstance(value, dict):
        if len(value) > 10000:
            raise ValueError("Usage parser state too large")
        for key, item in value.items():
            _text(key, "state key")
            if not dynamic and key not in _STATE_KEYS:
                raise ValueError(f"Unsupported usage state field: {key}")
            _state(item, dynamic=key in {"turn_models", "models", "turns"}, depth=depth + 1)
    elif value is not None and not isinstance(value, bool):
        if isinstance(value, int):
            _integer(value, "state counter")
        else:
            _text(value, "state value", limit=4096)
    return value


class UsageStore:
    """Atomic usage events and resumable file cursors in memory.sqlite3."""

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self.store = Store(data_dir)
        try:
            with self.store._lock:
                self.store._write(self._initialize)
        except Exception:
            self.store.close()
            raise

    def _initialize(self) -> None:
        statements = [
            "CREATE TABLE IF NOT EXISTS usage_sessions (thread_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, parent_thread_id TEXT, project TEXT NOT NULL, agent_path TEXT, agent_role TEXT, agent_nickname TEXT, thread_source TEXT, model_provider TEXT, started_at TEXT)",
            "CREATE TABLE IF NOT EXISTS usage_events (event_key TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES usage_sessions(thread_id), session_id TEXT NOT NULL, turn_id TEXT, root_turn_id TEXT, response_id TEXT, model TEXT, model_source TEXT, model_provider TEXT, service_tier TEXT, recorded_at TEXT, source_kind TEXT NOT NULL, input_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL, cache_write_input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, reasoning_output_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL, quality TEXT)",
            "CREATE UNIQUE INDEX IF NOT EXISTS usage_response_identity ON usage_events(thread_id,response_id) WHERE response_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS usage_root_session ON usage_events(session_id,thread_id,model)",
            "CREATE TABLE IF NOT EXISTS usage_scan_files (path TEXT PRIMARY KEY, offset INTEGER NOT NULL, file_identity TEXT, fingerprint TEXT, parser_state TEXT NOT NULL)",
        ]
        for statement in statements:
            self.store._connection.execute(statement)

    def __enter__(self) -> "UsageStore":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        self.store.close()

    def get_checkpoint(self, path: str | Path) -> dict[str, Any] | None:
        with self.store._lock:
            row = self.store._connection.execute("SELECT * FROM usage_scan_files WHERE path=?", (str(path),)).fetchone()
            return self._checkpoint(row) if row else None

    @staticmethod
    def _checkpoint(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["parser_state"] = json.loads(result["parser_state"])
        return result

    def list_checkpoints(self) -> list[dict[str, Any]]:
        with self.store._lock:
            return [self._checkpoint(row) for row in self.store._connection.execute("SELECT * FROM usage_scan_files ORDER BY path")]

    list_files = list_checkpoints

    def commit_scan(self, path: str | Path, expected_offset: int | None, checkpoint: dict[str, Any], session: dict[str, Any], events: list[dict[str, Any]]) -> bool:
        """Commit events and cursor together, or return False after a cursor race.

        A lower new offset is allowed for truncation/replay. Response identities
        remain durable, so replay cannot charge the same response twice.
        """
        path = _text(str(path), "path", required=True, limit=4096)
        if expected_offset is not None:
            _integer(expected_offset, "expected_offset")
        offset = _integer(checkpoint["offset"], "offset")
        identity = _text(checkpoint.get("file_identity"), "file_identity")
        fingerprint = _text(checkpoint.get("fingerprint"), "fingerprint")
        state = json.dumps(_state(checkpoint.get("parser_state", {})), separators=(",", ":"), ensure_ascii=False)
        if len(state) > 2_000_000:
            raise ValueError("Usage parser state too large")
        normalized = {key: _text(session.get(key), key, required=key in {"thread_id", "session_id", "project"}, limit=4096 if key == "project" else 512) for key in SESSION_FIELDS}
        normalized["project"] = project_key(normalized["project"])
        normalized["started_at"] = _timestamp(session.get("started_at"), "started_at")
        clean_events = []
        for event in events:
            clean = {key: _integer(event.get(key), key) if key in COUNTERS else _text(event.get(key), key, required=key in {"event_key", "thread_id", "session_id", "source_kind"}) for key in EVENT_FIELDS}
            if clean["thread_id"] != normalized["thread_id"] or clean["session_id"] != normalized["session_id"]:
                raise ValueError("Usage event owner mismatch")
            if clean["source_kind"] not in {"response", "legacy"}:
                raise ValueError("Unknown usage source kind")
            if clean["source_kind"] == "response" and not clean["response_id"]:
                raise ValueError("Precise usage requires response identity")
            clean["recorded_at"] = _timestamp(event.get("recorded_at"), "recorded_at")
            if clean["cached_input_tokens"] + clean["cache_write_input_tokens"] > clean["input_tokens"] or clean["reasoning_output_tokens"] > clean["output_tokens"] or clean["total_tokens"] != clean["input_tokens"] + clean["output_tokens"]:
                raise ValueError("Inconsistent usage counters")
            clean_events.append(clean)

        def operation() -> bool:
            conn = self.store._connection
            prior = conn.execute("SELECT offset FROM usage_scan_files WHERE path=?", (path,)).fetchone()
            if (prior is None and expected_offset not in (None, 0)) or (prior is not None and prior[0] != expected_offset):
                return False
            resolved_session = dict(normalized)
            previous_session = conn.execute("SELECT session_id FROM usage_sessions WHERE thread_id=?", (normalized["thread_id"],)).fetchone()
            # An identified parent task wins over a stale file's self-root
            # fallback, and conflicting explicit roots do not rewrite history.
            if previous_session and previous_session[0] != normalized["thread_id"]:
                resolved_session["session_id"] = previous_session[0]
            assignments = ",".join(f"{key}=COALESCE(excluded.{key},usage_sessions.{key})" for key in SESSION_FIELDS if key != "thread_id")
            conn.execute(f"INSERT INTO usage_sessions ({','.join(SESSION_FIELDS)}) VALUES ({','.join('?' for _ in SESSION_FIELDS)}) ON CONFLICT(thread_id) DO UPDATE SET {assignments}", tuple(resolved_session[key] for key in SESSION_FIELDS))
            if resolved_session["session_id"] != normalized["thread_id"]:
                conn.execute("UPDATE usage_events SET session_id=? WHERE thread_id=? AND session_id=thread_id", (resolved_session["session_id"], normalized["thread_id"]))
            has_precise = any(event["source_kind"] == "response" for event in clean_events) or conn.execute("SELECT 1 FROM usage_events WHERE thread_id=? AND source_kind='response' LIMIT 1", (normalized["thread_id"],)).fetchone() is not None
            if has_precise:
                conn.execute("DELETE FROM usage_events WHERE thread_id=? AND source_kind='legacy'", (normalized["thread_id"],))
            for event in clean_events:
                if has_precise and event["source_kind"] == "legacy":
                    continue
                event = dict(event, session_id=resolved_session["session_id"])
                # Replay may fill missing attribution but cannot rewrite counters.
                conn.execute(f"INSERT INTO usage_events ({','.join(EVENT_FIELDS)}) VALUES ({','.join('?' for _ in EVENT_FIELDS)}) ON CONFLICT DO UPDATE SET model=COALESCE(usage_events.model,excluded.model),model_provider=COALESCE(usage_events.model_provider,excluded.model_provider),service_tier=COALESCE(usage_events.service_tier,excluded.service_tier),model_source=CASE WHEN usage_events.model IS NULL AND excluded.model IS NOT NULL THEN excluded.model_source ELSE COALESCE(usage_events.model_source,excluded.model_source) END", tuple(event[key] for key in EVENT_FIELDS))
            conn.execute("INSERT INTO usage_scan_files VALUES (?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET offset=excluded.offset,file_identity=excluded.file_identity,fingerprint=excluded.fingerprint,parser_state=excluded.parser_state", (path, offset, identity, fingerprint, state))
            return True

        with self.store._lock:
            return self.store._write(operation)

    def usage_totals(self, project: str | Path | None = None, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return groups per root task, actual agent thread, model/provider/tier."""
        filters, args = [], []
        if project is not None:
            filters.append("s.project=?")
            args.append(project_key(project))
        if session_id is not None:
            filters.append("e.session_id=?")
            args.append(_text(session_id, "session_id", required=True))
        where = " WHERE " + " AND ".join(filters) if filters else ""
        sql = "SELECT s.project,e.session_id,e.thread_id,s.parent_thread_id,s.agent_path,s.agent_role,s.agent_nickname,s.thread_source,e.model,e.model_provider,e.service_tier,e.source_kind,e.quality,COUNT(*) AS event_count," + ",".join(f"SUM(e.{key}) AS {key}" for key in COUNTERS) + " FROM usage_events e JOIN usage_sessions s ON s.thread_id=e.thread_id" + where + " GROUP BY s.project,e.session_id,e.thread_id,e.model,e.model_provider,e.service_tier,e.source_kind,e.quality ORDER BY e.session_id,e.thread_id,e.model"
        with self.store._lock:
            return [dict(row) for row in self.store._connection.execute(sql, args)]
