"""Metadata-only usage ledger, separate from searchable memory content.

Input includes cached input; output includes reasoning output. Subset counters
must never be added again when calculating future task costs.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .store import Store, project_key

COUNTERS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
SESSION_FIELDS = ("thread_id", "session_id", "parent_thread_id", "project", "agent_path", "agent_role", "agent_nickname", "thread_source", "model_provider", "started_at")
EVENT_FIELDS = ("event_key", "thread_id", "session_id", "turn_id", "root_turn_id", "response_id", "model", "model_source", "model_provider", "service_tier", "requested_service_tier", "requested_service_tier_source", "service_tier_source", "recorded_at", "source_kind", *COUNTERS, "quality")
_STATE_KEYS = set(SESSION_FIELDS + COUNTERS) | {"owner", "session", "models", "turn_models", "current_model", "current_turn_id", "legacy_totals", "native_seen", "model", "model_source", "service_tier", "requested_service_tier", "requested_service_tier_source", "service_tier_source", "turn_id", "root_turn_id", "turns", "legacy_total", "skip_line", "invalid_owner", "inherited", "root_explicit", "parser_version", "malformed_records", "skipped_records", "source_mtime_ns"}


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
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return None


def period_bounds(start_at=None, end_at=None):
    """Validate an inclusive start and exclusive end, preserving instant meaning."""
    start = _timestamp(start_at, "start_at")
    end = _timestamp(end_at, "end_at")
    if start is not None and end is not None and start >= end:
        raise ValueError("Usage start_at must precede end_at")
    return start, end


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
            "CREATE TABLE IF NOT EXISTS usage_discovery_queue (scope TEXT NOT NULL,path TEXT NOT NULL,kind TEXT NOT NULL,PRIMARY KEY(scope,path))",
            "CREATE INDEX IF NOT EXISTS usage_discovery_next ON usage_discovery_queue(scope,kind,path)",
        ]
        for statement in statements:
            self.store._connection.execute(statement)
        conn = self.store._connection
        columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_events)")}
        for name in ("requested_service_tier", "requested_service_tier_source", "service_tier_source"):
            if name not in columns:
                conn.execute(f"ALTER TABLE usage_events ADD COLUMN {name} TEXT")
        if "requested_service_tier" not in columns:
            # Before parser v2 this field came exclusively from thread settings.
            # Keep the evidence, but never relabel a request as a provider result.
            conn.execute("UPDATE usage_events SET requested_service_tier=service_tier,requested_service_tier_source=CASE WHEN service_tier IS NOT NULL THEN 'thread_settings_legacy' END,service_tier=NULL")
            # One metadata-only migration makes lexical ranges exact even at
            # microsecond boundaries; julianday rounds values to milliseconds.
            for row in conn.execute("SELECT event_key,recorded_at FROM usage_events WHERE recorded_at IS NOT NULL").fetchall():
                try:
                    stamp = _timestamp(row[1], "recorded_at")
                except ValueError:
                    stamp = None
                conn.execute("UPDATE usage_events SET recorded_at=? WHERE event_key=?", (stamp, row[0]))
        conn.execute("CREATE INDEX IF NOT EXISTS usage_recorded_time ON usage_events(julianday(recorded_at))")
        conn.execute("CREATE INDEX IF NOT EXISTS usage_recorded_at ON usage_events(recorded_at)")
        # An old daemon may finish a scan while the new plugin is starting.
        # Its unproven settings tier must retain the old meaning after migration.
        for operation in ("INSERT", "UPDATE"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS usage_tier_provenance_{operation.lower()} AFTER {operation} ON usage_events WHEN NEW.service_tier IS NOT NULL AND NEW.service_tier_source IS NULL BEGIN UPDATE usage_events SET requested_service_tier=COALESCE(requested_service_tier,NEW.service_tier),requested_service_tier_source=COALESCE(requested_service_tier_source,'thread_settings_legacy'),service_tier=NULL WHERE event_key=NEW.event_key; END")

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

    def discovery_next(self, scope, roots=(), *, kind=None, after_path=None):
        """Persistent metadata work queue used by short-lived explicit refreshes."""
        def operation():
            conn = self.store._connection
            if roots and not conn.execute("SELECT 1 FROM usage_discovery_queue WHERE scope=? AND kind!='overflow' LIMIT 1", (scope,)).fetchone():
                # A capped snapshot is retried in a later bounded cycle; it
                # must not suppress discovery of new files in other directories.
                conn.execute("UPDATE usage_discovery_queue SET kind='directory' WHERE scope=? AND kind='overflow'", (scope,))
                conn.executemany("INSERT OR IGNORE INTO usage_discovery_queue VALUES (?,?,'directory')", [(scope, str(path)) for path in roots])
            filters, args = ["scope=?", "kind!='overflow'"], [scope]
            if kind is not None:
                filters.append("kind=?")
                args.append(kind)
            if after_path is not None:
                filters.append("path>?")
                args.append(str(after_path))
            row = conn.execute("SELECT path,kind FROM usage_discovery_queue WHERE " + " AND ".join(filters) + " ORDER BY path LIMIT 1", args).fetchone()
            return dict(row) if row else None
        with self.store._lock:
            return self.store._write(operation) if roots else operation()

    def discovery_finish(self, scope, path, children=(), *, overflow=False):
        """Replace a directory with its metadata snapshot, or acknowledge a file."""
        def operation():
            conn = self.store._connection
            conn.execute("DELETE FROM usage_discovery_queue WHERE scope=? AND path=?", (scope, str(path)))
            conn.executemany("INSERT OR IGNORE INTO usage_discovery_queue VALUES (?,?,?)", [(scope, str(child), kind) for child, kind in children])
            if overflow:
                conn.execute("INSERT OR IGNORE INTO usage_discovery_queue VALUES (?,?,'overflow')", (scope, str(path)))
        with self.store._lock:
            self.store._write(operation)

    def discovery_pending(self, scope):
        with self.store._lock:
            return {row[0]: row[1] for row in self.store._connection.execute("SELECT kind,COUNT(*) FROM usage_discovery_queue WHERE scope=? GROUP BY kind", (scope,))}

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
                enrich = ",".join(f"{key}=COALESCE(usage_events.{key},excluded.{key})" for key in ("model", "model_provider", "service_tier", "service_tier_source", "requested_service_tier", "requested_service_tier_source"))
                conn.execute(f"INSERT INTO usage_events ({','.join(EVENT_FIELDS)}) VALUES ({','.join('?' for _ in EVENT_FIELDS)}) ON CONFLICT DO UPDATE SET {enrich},quality=CASE WHEN excluded.quality LIKE '%partial_counters%' THEN excluded.quality ELSE COALESCE(usage_events.quality,excluded.quality) END,model_source=CASE WHEN usage_events.model IS NULL AND excluded.model IS NOT NULL THEN excluded.model_source ELSE COALESCE(usage_events.model_source,excluded.model_source) END", tuple(event[key] for key in EVENT_FIELDS))
            conn.execute("INSERT INTO usage_scan_files VALUES (?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET offset=excluded.offset,file_identity=excluded.file_identity,fingerprint=excluded.fingerprint,parser_state=excluded.parser_state", (path, offset, identity, fingerprint, state))
            return True

        with self.store._lock:
            return self.store._write(operation)

    @staticmethod
    def _filters(project=None, session_id=None, start_at=None, end_at=None):
        filters, args = [], []
        if project is not None:
            filters.append("s.project=?")
            args.append(project_key(project))
        if session_id is not None:
            filters.append("e.session_id=?")
            args.append(_text(session_id, "session_id", required=True))
        start, end = period_bounds(start_at, end_at)
        for value, comparison in ((start, ">="), (end, "<")):
            if value is not None:
                # julianday handles old writers' offsets/fraction widths. Its
                # millisecond rounding requires a guard and exact Python check.
                point = datetime.fromisoformat(value.replace("Z", "+00:00"))
                try:
                    point += timedelta(milliseconds=-1 if comparison == ">=" else 1)
                except OverflowError:
                    pass
                filters.append(f"julianday(e.recorded_at){comparison}julianday(?)")
                args.append(point.isoformat())
        return " WHERE " + " AND ".join(filters) if filters else "", args

    def list_events(self, project=None, session_id=None, *, start_at=None, end_at=None) -> list[dict[str, Any]]:
        """Read individual responses for pricing before aggregation; never content."""
        where, args = self._filters(project, session_id, start_at, end_at)
        sql = "SELECT e.*,s.project,s.parent_thread_id,s.agent_path,s.agent_role,s.agent_nickname,s.thread_source FROM usage_events e JOIN usage_sessions s ON s.thread_id=e.thread_id" + where + " ORDER BY e.recorded_at,e.event_key"
        with self.store._lock:
            rows = [dict(row) for row in self.store._connection.execute(sql, args)]
        start, end = period_bounds(start_at, end_at)
        if start is None and end is None:
            return rows
        result = []
        for row in rows:
            try:
                stamp = _timestamp(row["recorded_at"], "recorded_at")
            except ValueError:
                continue
            if stamp is not None and (start is None or stamp >= start) and (end is None or stamp < end):
                row["recorded_at"] = stamp
                result.append(row)
        return sorted(result, key=lambda row: (row["recorded_at"], row["event_key"]))

    def usage_totals(self, project: str | Path | None = None, session_id: str | None = None, *, start_at=None, end_at=None) -> list[dict[str, Any]]:
        """Return groups per root task, actual agent thread, model/provider/tier."""
        dimensions = "s.project,e.session_id,e.thread_id,e.model,e.model_provider,e.service_tier,e.requested_service_tier,e.requested_service_tier_source,e.service_tier_source,e.source_kind,e.quality"
        if start_at is not None or end_at is not None:
            rows = self.list_events(project, session_id, start_at=start_at, end_at=end_at)
            fields = tuple(name.split(".")[1] for name in dimensions.split(","))
            groups = {}
            for row in rows:
                key = tuple(row[name] for name in fields)
                if key not in groups:
                    groups[key] = {name: row.get(name) for name in (*fields, "parent_thread_id", "agent_path", "agent_role", "agent_nickname", "thread_source")}
                    groups[key].update(event_count=0, **{name: 0 for name in COUNTERS})
                groups[key]["event_count"] += 1
                for name in COUNTERS:
                    groups[key][name] += row[name]
            return sorted(groups.values(), key=lambda row: (row["session_id"], row["thread_id"], row["model"] or ""))
        where, args = self._filters(project, session_id)
        sql = "SELECT " + dimensions + ",s.parent_thread_id,s.agent_path,s.agent_role,s.agent_nickname,s.thread_source,COUNT(*) AS event_count," + ",".join(f"SUM(e.{key}) AS {key}" for key in COUNTERS) + " FROM usage_events e JOIN usage_sessions s ON s.thread_id=e.thread_id" + where + " GROUP BY " + dimensions + " ORDER BY e.session_id,e.thread_id,e.model"
        with self.store._lock:
            return [dict(row) for row in self.store._connection.execute(sql, args)]

    def ledger_coverage(self, project=None, session_id=None, *, start_at=None, end_at=None):
        """Coverage of stored events only; filesystem completeness is separate."""
        where, args = self._filters(project, session_id, start_at, end_at)
        sql = "SELECT COUNT(*) AS event_count,MIN(e.recorded_at) AS first_recorded_at,MAX(e.recorded_at) AS last_recorded_at,COALESCE(SUM(e.model IS NULL),0) AS unknown_model_events,COALESCE(SUM(e.service_tier IS NULL AND e.requested_service_tier IS NULL),0) AS unknown_tier_events,COALESCE(SUM(e.service_tier IS NULL AND e.requested_service_tier IS NOT NULL),0) AS requested_tier_events,COALESCE(SUM(e.source_kind='legacy'),0) AS legacy_events FROM usage_events e JOIN usage_sessions s ON s.thread_id=e.thread_id" + where
        missing_where, missing_args = self._filters(project, session_id)
        missing_where += " AND " if missing_where else " WHERE "
        with self.store._lock:
            result = dict(self.store._connection.execute(sql, args).fetchone())
            result["undated_events_outside_period"] = self.store._connection.execute("SELECT COUNT(*) FROM usage_events e JOIN usage_sessions s ON s.thread_id=e.thread_id" + missing_where + "julianday(e.recorded_at) IS NULL", missing_args).fetchone()[0]
        if start_at is not None or end_at is not None:
            rows = self.list_events(project, session_id, start_at=start_at, end_at=end_at)
            result.update(event_count=len(rows), first_recorded_at=rows[0]["recorded_at"] if rows else None,
                          last_recorded_at=rows[-1]["recorded_at"] if rows else None,
                          unknown_model_events=sum(row["model"] is None for row in rows),
                          unknown_tier_events=sum(row["service_tier"] is None and row["requested_service_tier"] is None for row in rows),
                          requested_tier_events=sum(row["service_tier"] is None and row["requested_service_tier"] is not None for row in rows),
                          legacy_events=sum(row["source_kind"] == "legacy" for row in rows))
        return result
