"""Durable project memory: bounded evidence jobs and conservative state reduction.

All content is untrusted data. This module does not call a model. Transaction
helpers deliberately use the caller's transaction so capture/deletion is atomic.
"""
from __future__ import annotations

from .host import source_role

import hashlib
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .store import Store

PROCESSOR_VERSION = "project-memory-v4"
RECONSIDERATION_VERSION = "task-consolidation-v4"
TASK_ID_PATTERN = r"(?<![\w-])[A-Za-z][A-Za-z0-9]{1,15}-[0-9]{1,10}(?![\w-])"
RELATIONS = {"supports", "contradicts", "replaces", "closes", "summarizes"}
STATES = {"proposed", "accepted", "open", "blocked", "completed"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _role(source: str | None) -> str:
    return source_role(source)


def install_schema(connection: sqlite3.Connection) -> None:
    """Install additive tables, without backfilling historical observations."""
    had_fts = connection.execute("SELECT 1 FROM sqlite_master WHERE name='pm_objects_fts'").fetchone() is not None
    statements = (
        "CREATE TABLE IF NOT EXISTS pm_projects (project TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0, deletion_generation INTEGER NOT NULL DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS pm_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, source_id TEXT NOT NULL UNIQUE REFERENCES entries(id) ON DELETE CASCADE, session_id TEXT NOT NULL, offset INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0, salient INTEGER NOT NULL DEFAULT 0, blocked_job_id TEXT)",
        "CREATE INDEX IF NOT EXISTS pm_events_pending ON pm_events(project,done,session_id,seq)",
        "CREATE TABLE IF NOT EXISTS pm_jobs (id TEXT PRIMARY KEY, project TEXT NOT NULL, session_id TEXT NOT NULL, fingerprint TEXT NOT NULL, token TEXT NOT NULL, status TEXT NOT NULL, lease_until REAL NOT NULL, retry_after REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL, generation INTEGER NOT NULL, input_json TEXT NOT NULL, receipt_json TEXT, error TEXT)",
        "CREATE INDEX IF NOT EXISTS pm_jobs_lane ON pm_jobs(project,session_id,status)",
        "CREATE TABLE IF NOT EXISTS pm_objects (id TEXT PRIMARY KEY, project TEXT NOT NULL, object_type TEXT NOT NULL, session_id TEXT NOT NULL, revision INTEGER NOT NULL, data_json TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS pm_objects_project ON pm_objects(project,object_type,revision)",
        "CREATE TABLE IF NOT EXISTS pm_episode_heads (project TEXT NOT NULL, logical_episode_id TEXT NOT NULL, head_id TEXT, episode_version INTEGER NOT NULL, PRIMARY KEY(project,logical_episode_id))",
        "CREATE VIRTUAL TABLE IF NOT EXISTS pm_objects_fts USING fts5(id UNINDEXED,project UNINDEXED,search_text)",
        "CREATE TRIGGER IF NOT EXISTS pm_objects_ai AFTER INSERT ON pm_objects BEGIN INSERT INTO pm_objects_fts(id,project,search_text) VALUES (new.id,new.project,COALESCE(json_extract(new.data_json,'$.text'),'') || ' ' || COALESCE(json_extract(new.data_json,'$.summary'),'') || ' ' || COALESCE(json_extract(new.data_json,'$.goal'),'') || ' ' || COALESCE(json_extract(new.data_json,'$.rationale'),'') || ' ' || COALESCE(json_extract(new.data_json,'$.outcome'),'') || ' ' || COALESCE(json_extract(new.data_json,'$.open_work'),'')); END",
        "CREATE TRIGGER IF NOT EXISTS pm_objects_ad AFTER DELETE ON pm_objects BEGIN DELETE FROM pm_objects_fts WHERE id=old.id; END",
        "CREATE TABLE IF NOT EXISTS pm_evidence (object_id TEXT NOT NULL REFERENCES pm_objects(id) ON DELETE CASCADE, source_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE, PRIMARY KEY(object_id,source_id))",
        "CREATE INDEX IF NOT EXISTS pm_evidence_source ON pm_evidence(source_id)",
        "CREATE TABLE IF NOT EXISTS pm_relations (id TEXT PRIMARY KEY, project TEXT NOT NULL, from_id TEXT NOT NULL REFERENCES pm_objects(id) ON DELETE CASCADE, to_id TEXT NOT NULL REFERENCES pm_objects(id) ON DELETE CASCADE, type TEXT NOT NULL, data_json TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS pm_relation_evidence (relation_id TEXT NOT NULL REFERENCES pm_relations(id) ON DELETE CASCADE, source_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE, PRIMARY KEY(relation_id,source_id))",
        "CREATE TABLE IF NOT EXISTS pm_revisions (project TEXT NOT NULL, revision INTEGER NOT NULL, job_id TEXT, event_seq INTEGER, created_at REAL NOT NULL, PRIMARY KEY(project,revision))",
        "CREATE TABLE IF NOT EXISTS pm_usage_attempts (attempt_id TEXT PRIMARY KEY, project TEXT NOT NULL, session_id TEXT NOT NULL, job_id TEXT NOT NULL, claim_token TEXT NOT NULL, processor_id TEXT NOT NULL, model TEXT NOT NULL, reasoning_effort TEXT NOT NULL, started_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT, outcome TEXT NOT NULL DEFAULT 'running', error_code TEXT, worker_thread_id TEXT, worker_turn_id TEXT, duration_ms INTEGER, usage_status TEXT NOT NULL DEFAULT 'unknown', usage_source TEXT NOT NULL DEFAULT 'unknown', usage_updates INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER, cached_input_tokens INTEGER, cache_write_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, total_tokens INTEGER)",
        "CREATE INDEX IF NOT EXISTS pm_usage_attempts_project ON pm_usage_attempts(project,started_at,attempt_id)",
    )
    for statement in statements:
        connection.execute(statement)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(pm_usage_attempts)")}
    for name, declaration in {
        "requested_runtime": "TEXT", "requested_provider": "TEXT", "requested_model": "TEXT",
        "requested_reasoning_effort": "TEXT", "requested_timeout": "REAL",
        "runtime": "TEXT", "provider": "TEXT", "actual_model": "TEXT",
        "actual_reasoning_effort": "TEXT", "profile_basis": "TEXT",
    }.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE pm_usage_attempts ADD COLUMN {name} {declaration}")
    connection.execute("UPDATE pm_usage_attempts SET requested_runtime='codex',requested_provider='openai',requested_model=model,requested_reasoning_effort=reasoning_effort,profile_basis='legacy_requested_profile' WHERE profile_basis IS NULL")
    from .project_memory_custody import install_schema as install_custody_schema
    install_custody_schema(connection)
    if "blocked_job_id" not in {r[1] for r in connection.execute("PRAGMA table_info(pm_events)")}:
        connection.execute("ALTER TABLE pm_events ADD COLUMN blocked_job_id TEXT")
    if "salient" not in {r[1] for r in connection.execute("PRAGMA table_info(pm_events)")}:
        connection.execute("ALTER TABLE pm_events ADD COLUMN salient INTEGER NOT NULL DEFAULT 0")
    if not had_fts:
        connection.execute("INSERT INTO pm_objects_fts(id,project,search_text) SELECT o.id,o.project,COALESCE(json_extract(o.data_json,'$.text'),'') || ' ' || COALESCE(json_extract(o.data_json,'$.summary'),'') || ' ' || COALESCE(json_extract(o.data_json,'$.goal'),'') || ' ' || COALESCE(json_extract(o.data_json,'$.rationale'),'') FROM pm_objects o WHERE NOT EXISTS (SELECT 1 FROM pm_objects_fts f WHERE f.id=o.id)")


def capture_event(connection: sqlite3.Connection, entry_id: str) -> bool:
    """Capture a saved raw hook event; caller applies capture/privacy policy."""
    row = connection.execute("SELECT project,session_id,source,body FROM entries WHERE id=?", (entry_id,)).fetchone()
    if row is None or _role(row["source"]) not in {"user_intent", "assistant_report", "tool_record", "lifecycle_marker"}:
        return False
    connection.execute("INSERT OR IGNORE INTO pm_projects(project) VALUES (?)", (row["project"],))
    result = connection.execute(
        "INSERT OR IGNORE INTO pm_events(project,source_id,session_id,salient) VALUES (?,?,?,?)",
        (row["project"], entry_id, row["session_id"] or "", _salient_user_signal(row["body"]) if _role(row["source"]) == "user_intent" else 0),
    )
    return bool(result.rowcount)


def _delete_objects(connection: sqlite3.Connection, project: str, object_ids: set[str]) -> int:
    # Derived support/summarization dependants are invalidated transitively.
    pending = set(object_ids)
    while pending:
        current = pending.pop()
        for child in connection.execute("SELECT id FROM pm_objects WHERE project=? AND object_type='episode' AND json_extract(data_json,'$.parent_episode_id')=?", (project, current)):
            if child[0] not in object_ids:
                object_ids.add(child[0])
                pending.add(child[0])
        for row in connection.execute(
            "SELECT from_id,to_id,type FROM pm_relations WHERE project=? AND ((from_id=? AND type='supports') OR (to_id=? AND type='summarizes'))",
            (project, current, current),
        ):
            dependent = row["to_id"] if row["type"] == "supports" else row["from_id"]
            if dependent not in object_ids:
                object_ids.add(dependent)
                pending.add(dependent)
    for object_id in object_ids:
        connection.execute("UPDATE pm_episode_heads SET head_id=NULL WHERE project=? AND head_id=?", (project, object_id))
        connection.execute("DELETE FROM pm_objects WHERE project=? AND id=?", (project, object_id))
    return len(object_ids)


def revoke_sources(connection: sqlite3.Connection, project: str, source_ids: list[str]) -> dict[str, Any]:
    """Erase dependent text and fence every in-flight project job before deletion."""
    from .store import project_key
    project = project_key(project)
    source_ids = list(dict.fromkeys(source_ids))
    if not source_ids:
        return {"deleted_objects": 0}
    objects: set[str] = set()
    for source_id in source_ids:
        objects.update(r[0] for r in connection.execute(
            "SELECT e.object_id FROM pm_evidence e JOIN pm_objects o ON o.id=e.object_id WHERE o.project=? AND e.source_id=?", (project, source_id)))
        for relation in connection.execute(
            "SELECT r.from_id,r.to_id,r.type FROM pm_relations r JOIN pm_relation_evidence e ON e.relation_id=r.id WHERE r.project=? AND e.source_id=? AND r.type IN ('supports','summarizes')",
            (project, source_id)):
            objects.add(relation["to_id"] if relation["type"] == "supports" else relation["from_id"])
    # Traverse dependency edges before removing their evidence/relations.
    count = _delete_objects(connection, project, objects)
    for source_id in source_ids:
        connection.execute("DELETE FROM pm_relations WHERE project=? AND id IN (SELECT relation_id FROM pm_relation_evidence WHERE source_id=?)", (project, source_id))
        connection.execute("DELETE FROM pm_events WHERE project=? AND source_id=?", (project, source_id))
    connection.execute("UPDATE pm_projects SET revision=revision+1,deletion_generation=deletion_generation+1 WHERE project=?", (project,))
    # Job snapshots contain raw text and must also be erased. No stale output can
    # commit without its durable job/token. Revision records contain no content.
    connection.execute("DELETE FROM pm_jobs WHERE project=?", (project,))
    connection.execute("UPDATE pm_events SET blocked_job_id=NULL WHERE project=?", (project,))
    return {"deleted_objects": count}


def protected_source_ids(connection: sqlite3.Connection, project: str) -> set[str]:
    """Retain sources backing live memory and pending work during age pruning."""
    from .store import project_key
    project = project_key(project)
    from .project_memory_custody import protected_source_ids as custody_sources
    return custody_sources(connection, project) | {r[0] for r in connection.execute(
        "SELECT e.source_id FROM pm_evidence e JOIN pm_objects o ON o.id=e.object_id WHERE o.project=? "
        "UNION SELECT e.source_id FROM pm_relation_evidence e JOIN pm_relations r ON r.id=e.relation_id WHERE r.project=? "
        "UNION SELECT source_id FROM pm_events WHERE project=? AND done=0", (project, project, project))}


class ProjectMemory:
    def __init__(self, store: Store):
        self.store = store

    def _project(self, project: Any) -> str:
        return self.store.project_key(project)

    def _coverage(self, project: str) -> dict[str, Any]:
        c = self.store._connection
        row = c.execute("SELECT COUNT(*) n,COALESCE(SUM(done),0) done,COALESCE(MAX(seq),0) latest,COALESCE(SUM(CASE WHEN offset>0 AND done=0 THEN 1 ELSE 0 END),0) fragmented,COALESCE(SUM(CASE WHEN blocked_job_id IS NOT NULL AND done=0 THEN 1 ELSE 0 END),0) quarantined FROM pm_events WHERE project=?", (project,)).fetchone()
        first = c.execute("SELECT MIN(seq) FROM pm_events WHERE project=? AND done=0", (project,)).fetchone()[0]
        cursor = c.execute("SELECT COALESCE(MAX(seq),0) FROM pm_events WHERE project=? AND done=1 AND (? IS NULL OR seq<?)", (project, first, first)).fetchone()[0]
        jobs = {r["status"]: r["n"] for r in c.execute("SELECT status,COUNT(*) n FROM pm_jobs WHERE project=? GROUP BY status", (project,))}
        omitted = c.execute("SELECT COALESCE(SUM(COALESCE(json_extract(receipt_json,'$.omitted_transitions_count'),0)),0) FROM pm_jobs WHERE project=? AND status='completed'", (project,)).fetchone()[0]
        downgraded = c.execute("SELECT COALESCE(SUM(COALESCE(json_extract(receipt_json,'$.downgraded_states_count'),0)),0) FROM pm_jobs WHERE project=? AND status='completed'", (project,)).fetchone()[0]
        continuities = c.execute("SELECT COALESCE(SUM(COALESCE(json_extract(receipt_json,'$.omitted_continuities_count'),0)),0) FROM pm_jobs WHERE project=? AND status='completed'", (project,)).fetchone()[0]
        historical_context = c.execute("SELECT COUNT(*) FROM pm_objects o JOIN pm_episode_heads h ON h.project=o.project AND h.head_id=o.id AND h.logical_episode_id=json_extract(o.data_json,'$.logical_episode_id') WHERE o.project=? AND json_extract(o.data_json,'$.prior_context_status') IS NOT NULL", (project,)).fetchone()[0]
        discovery = {}
        reconsideration = self._reconsideration(project, include_completed=True, discovery=discovery)
        reconsideration_coverage = reconsideration["reconsideration"] if reconsideration else {
            "candidate_window_bounded": True, "head_limit": 100, "complete_history": False,
            "candidate_head_ids": [], "omitted_sources": 0, **discovery}
        return {"historical_context_heads": historical_context, "reconsideration": reconsideration_coverage, "omitted_continuities_count": continuities, "omitted_transitions_count": omitted, "downgraded_states_count": downgraded, "pending_reconciliation": bool(omitted or downgraded or continuities or reconsideration or historical_context or discovery.get("identity_sources_omitted") or discovery.get("ambiguous_heads")), "complete_transcript": False, "observations_only": True, "event_time_basis": "source_capture_time", "partially_processed_events": row["fragmented"], "quarantined_events": row["quarantined"], "coverage_gaps": bool(row["quarantined"] or omitted or downgraded or continuities or reconsideration or discovery.get("identity_sources_omitted")), "captured_events": row["n"], "processed_events": row["done"], "pending_events": row["n"]-row["done"], "latest_event_seq": row["latest"], "processed_through_seq": cursor, "running_jobs": jobs.get("running", 0), "failed_jobs": jobs.get("failed", 0), "quarantined_jobs": jobs.get("quarantined", 0)}

    def _state(self, project: str, limit: int, signal_limit: int, exclude_session_id: str | None = None, query: str = "", environment: str | None = None, version: str | None = None, topic_query: bool = False, include_episode_history: bool = False, include_project_obligations: bool = False, include_coverage: bool = True) -> dict[str, Any]:
        c = self.store._connection
        row = c.execute("SELECT * FROM pm_projects WHERE project=?", (project,)).fetchone()
        objects = []
        # Independent lanes prevent old obligations from crowding every episode
        # out of retrieval. Active state is ranked before each lane's limit.
        for object_type in ("claim", "episode"):
            filters = ""
            filter_params = []
            if object_type == "episode" and not include_episode_history:
                filters += " AND (EXISTS (SELECT 1 FROM pm_episode_heads h WHERE h.project=o.project AND h.logical_episode_id=COALESCE(json_extract(o.data_json,'$.logical_episode_id'),o.id) AND h.head_id=o.id) OR (NOT EXISTS (SELECT 1 FROM pm_episode_heads h WHERE h.project=o.project AND h.logical_episode_id=COALESCE(json_extract(o.data_json,'$.logical_episode_id'),o.id)) AND COALESCE(json_extract(o.data_json,'$.logical_episode_id'),o.id)=o.id))"
            fts_query = _fts_query(query, project, topic_query=topic_query)
            if fts_query:
                filters += " AND o.id IN (SELECT id FROM pm_objects_fts WHERE pm_objects_fts MATCH ? AND project=?)"
                filter_params.extend((fts_query, project))
            if object_type == "claim" and environment is not None:
                filters += " AND (COALESCE(json_extract(o.data_json,'$.scope.environment'),'')='' OR CASE lower(json_extract(o.data_json,'$.scope.environment')) WHEN 'prod' THEN 'production' WHEN 'dev' THEN 'development' ELSE lower(json_extract(o.data_json,'$.scope.environment')) END=?)"
                filter_params.append({"prod": "production", "dev": "development"}.get(environment.casefold(), environment.casefold()))
            if object_type == "claim" and version is not None:
                filters += " AND (COALESCE(json_extract(o.data_json,'$.scope.version'),'')='' OR ltrim(lower(json_extract(o.data_json,'$.scope.version')),'v')=?)"
                filter_params.append(version.casefold().lstrip("v"))
            records = c.execute(
                "SELECT o.data_json, CASE WHEN EXISTS (SELECT 1 FROM pm_relations r WHERE r.project=o.project AND r.to_id=o.id AND r.type IN ('closes','replaces') AND (? IS NULL OR NOT EXISTS (SELECT 1 FROM pm_relation_evidence re JOIN entries e ON e.id=re.source_id WHERE re.relation_id=r.id AND e.session_id=?))) THEN 1 ELSE 0 END transitioned "
                "FROM pm_objects o WHERE o.project=? AND o.object_type=? AND (? IS NULL OR (o.session_id!=? AND NOT EXISTS (SELECT 1 FROM pm_evidence p JOIN entries e ON e.id=p.source_id WHERE p.object_id=o.id AND e.session_id=?))) "
                + filters + " ORDER BY CASE WHEN o.object_type='claim' AND transitioned=0 AND json_extract(o.data_json,'$.workflow_state') IN ('open','blocked','accepted') THEN 0 ELSE 1 END, CASE WHEN o.object_type='claim' AND transitioned=0 AND json_extract(o.data_json,'$.workflow_state') IN ('open','blocked','accepted') THEN o.revision ELSE -o.revision END,o.id LIMIT ?",
                (exclude_session_id, exclude_session_id, project, object_type, exclude_session_id, exclude_session_id, exclude_session_id, *filter_params, limit))
            objects.extend(json.loads(r["data_json"]) for r in records)
        ids = {o["id"] for o in objects}
        relations = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            relations = [json.loads(r[0]) for r in c.execute(
                f"SELECT r.data_json FROM pm_relations r WHERE r.project=? AND (r.from_id IN ({placeholders}) OR r.to_id IN ({placeholders})) AND (? IS NULL OR NOT EXISTS (SELECT 1 FROM pm_relation_evidence p JOIN entries e ON e.id=p.source_id WHERE p.relation_id=r.id AND e.session_id=?)) ORDER BY r.id LIMIT ?",
                (project, *ids, *ids, exclude_session_id, exclude_session_id, limit*4))]
        # State is computed from immutable base claims. Revoking transition
        # evidence therefore restores an original open requirement.
        for obj in objects:
            _capture_order(c, project, obj)
            if obj["object_type"] == "episode":
                _episode_identity(c, project, obj)
            obj["base_workflow_state"] = obj.get("workflow_state")
            if obj["object_type"] == "claim":
                # Project transitions before rendering; the relation display
                # limit must never resurrect already closed work.
                _apply_transitions(c, project, obj, exclude_session_id)
        for relation in relations:
            relation["evidence"], relation["evidence_omitted"] = _bounded_evidence(relation["evidence"])
        signals = self._signals(project, signal_limit, exclude_session_id)
        from .project_memory_custody import snapshot as custody_snapshot
        custody, custody_coverage = custody_snapshot(c, project, signal_limit, exclude_session_id,
            fts_query=_fts_query(query, project, topic_query=topic_query))
        coverage = self._coverage(project) if include_coverage else {"pending_events": 0}
        obligations = _project_obligations(c, project, min(limit, 32), exclude_session_id) if include_project_obligations else []
        return {"intent_custody": custody, "intent_custody_coverage": custody_coverage, "project_obligations": obligations, "project": project, "revision": row["revision"] if row else 0, "deletion_generation": row["deletion_generation"] if row else 0, "claims": [o for o in objects if o["object_type"] == "claim"], "episodes": [o for o in objects if o["object_type"] == "episode"], "relations": relations, "provisional_signals": signals, "coverage": coverage, "pending": bool(coverage["pending_events"])}

    def _signals(self, project: str, limit: int, exclude_session_id: str | None) -> list[dict[str, Any]]:
        if not limit:
            return []
        connection = self.store._connection
        ranked = []
        for source in connection.execute("SELECT e.*,v.seq,v.salient FROM pm_events v JOIN entries e ON e.id=v.source_id WHERE v.project=? AND v.done=0 AND (? IS NULL OR e.session_id IS NULL OR e.session_id!=?) AND (codex_mem_source_role(e.source)='user_intent') ORDER BY v.salient DESC,CASE WHEN v.salient>=5 THEN -v.seq WHEN v.salient>0 THEN v.seq ELSE -v.seq END LIMIT ?", (project, exclude_session_id, exclude_session_id, limit)):
            salient = source["salient"]
            signal = {"id": source["id"], "source_id": source["id"], "text": source["body"][:1200], "truncated": len(source["body"])>1200, "source_role": "user_intent", "provisional": True, "signal_kind": _user_signal_kind(source["body"]), "selection_basis": "explicit_user_marker" if salient else "recent_user_message", "seq": source["seq"], "session_id": source["session_id"]}
            rank = (0, -salient, -source["seq"] if salient>=5 else source["seq"]) if salient else (2, 0, -source["seq"])
            ranked.append((rank, signal))
        # A saved execution side-index row, not prose that merely says "Tool",
        # establishes this provenance. Parse only a bounded recent candidate set.
        placeholders = ",".join("?" for _ in _EXECUTION_TOOLS)
        rows = connection.execute(
            f"SELECT e.*,v.seq FROM pm_events v JOIN entries e ON e.id=v.source_id JOIN tool_uses t ON t.entry_id=e.id AND t.project=v.project AND t.session_id=e.session_id WHERE v.project=? AND v.done=0 AND (? IS NULL OR e.session_id!=?) AND (codex_mem_source_role(e.source)='tool_record') AND lower(t.tool_name) IN ({placeholders}) AND json_type(t.tool_input)='object' AND json_type(t.tool_response)='object' AND COALESCE(json_extract(t.input_metadata_json,'$.truncated'),0)=0 AND COALESCE(json_extract(t.response_metadata_json,'$.truncated'),0)=0 AND COALESCE(json_extract(t.input_metadata_json,'$.excluded'),0)=0 AND COALESCE(json_extract(t.response_metadata_json,'$.excluded'),0)=0 ORDER BY v.seq DESC LIMIT ?",
            (project, exclude_session_id, exclude_session_id, *sorted(_EXECUTION_TOOLS), min(32, limit*4)))
        seen = set()
        for source in rows:
            if source["id"] in seen:
                continue
            seen.add(source["id"])
            signal = _execution_result_signal(connection, project, source)
            if signal:
                ranked.append(((1, 0, -source["seq"]), signal))
        ranked.sort(key=lambda pair: pair[0])
        return [signal for _, signal in ranked[:limit]]

    def snapshot(self, project: Any, limit: int = 100, signal_limit: int = 8, exclude_session_id: str | None = None, query: str = "", environment: str | None = None, version: str | None = None, topic_query: bool = False, include_episode_history: bool = False, include_project_obligations: bool = False) -> dict[str, Any]:
        workspace = self._project(project)
        with self.store._lock:
            return self.store._read(lambda: self._state(workspace, max(1, min(int(limit), 1000)), max(0, min(int(signal_limit), 32)), exclude_session_id, query, environment, version, topic_query, include_episode_history, include_project_obligations))

    def pending_projects(self, limit: int = 20, projects: list[str] | None = None, ready_only: bool = False) -> list[str]:
        now = time.time()
        with self.store._lock:
            def read():
                params: list[Any] = [now, now]
                sql = "SELECT v.project,MIN(v.seq) first FROM pm_events v WHERE v.done=0 AND v.blocked_job_id IS NULL AND NOT EXISTS (SELECT 1 FROM pm_jobs j WHERE j.project=v.project AND j.session_id=v.session_id AND ((j.status='running' AND j.lease_until>?) OR (j.status='failed' AND j.retry_after>?)))"
                if ready_only:
                    sql += _readiness_sql()
                    params.extend((now-30, now-120, now-5))
                if projects is not None:
                    if not projects:
                        return []
                    keys = list(dict.fromkeys(self._project(p) for p in projects))
                    sql += " AND v.project IN (" + ",".join("?" for _ in keys) + ")"
                    params.extend(keys)
                sql += " GROUP BY v.project ORDER BY first LIMIT ?"
                params.append(max(1, min(int(limit), 1000)))
                found = [r[0] for r in self.store._connection.execute(sql, params)]
                candidates = keys if projects is not None else [r[0] for r in self.store._connection.execute("SELECT project FROM pm_projects ORDER BY project LIMIT 100")]
                for candidate in candidates:
                    if len(found) >= max(1, min(int(limit), 1000)):
                        break
                    if candidate not in found and self._reconsideration(candidate) is not None:
                        found.append(candidate)
                return found
            return self.store._read(read)

    def claim(self, project: Any, max_events: int = 32, max_chars: int = 24000, lease_seconds: int = 120, ready_only: bool = False) -> dict[str, Any] | None:
        project = self._project(project)
        max_events, max_chars = max(1, min(int(max_events), 128)), max(256, min(int(max_chars), 120000))
        now = time.time()
        with self.store._lock:
            def claim_job():
                c = self.store._connection
                lane_sql = "SELECT v.session_id FROM pm_events v WHERE v.project=? AND v.done=0 AND v.blocked_job_id IS NULL AND NOT EXISTS (SELECT 1 FROM pm_jobs j WHERE j.project=v.project AND j.session_id=v.session_id AND ((j.status='running' AND j.lease_until>?) OR (j.status='failed' AND j.retry_after>?)))"
                lane_params = [project, now, now]
                if ready_only:
                    lane_sql += _readiness_sql()
                    lane_params.extend((now-30, now-120, now-5))
                lane = c.execute(lane_sql+" ORDER BY v.seq LIMIT 1", lane_params).fetchone()
                if lane is None:
                    return self._claim_reconsideration(project, now, lease_seconds, max_events, max_chars)
                sources = []
                remaining = max_chars
                for row in c.execute("SELECT e.*,v.seq,v.offset FROM pm_events v JOIN entries e ON e.id=v.source_id WHERE v.project=? AND v.session_id=? AND v.done=0 AND v.blocked_job_id IS NULL ORDER BY v.seq LIMIT ?", (project, lane[0], max_events)):
                    if remaining <= 0:
                        break
                    body = row["body"][row["offset"]:row["offset"]+remaining]
                    sources.append({k: row[k] for k in ("id", "title", "source", "session_id", "turn_id", "created_at", "seq") } | {"body": body, "body_offset": row["offset"], "body_length": len(row["body"]), "source_role": _role(row["source"]), "truncated": row["offset"]+len(body)<len(row["body"])})
                    remaining -= len(body)
                state = _bounded_generation_state(self._state(project, 100, 0, include_project_obligations=True), sources)
                historical_context = self._disclose_history(project, state, sources, max_chars, max_events)
                fingerprint = _hash({"sources": sources, "revision": state["revision"], "generation": state["deletion_generation"], "processor_version": PROCESSOR_VERSION})
                job_id = "pmj_" + _hash([project, lane[0], fingerprint])[:40]
                prior = c.execute("SELECT attempts FROM pm_jobs WHERE id=?", (job_id,)).fetchone()
                token = uuid.uuid4().hex
                snapshot = {"job_id": job_id, "token": token, "project": project, "session_id": lane[0], "revision": state["revision"], "deletion_generation": state["deletion_generation"], "input_fingerprint": fingerprint, "processor_version": PROCESSOR_VERSION, "sources": sources, "state": state, "historical_context": historical_context}
                # Expired work in this lane is revoked even if changed state or
                # new events produced a different deterministic job identifier.
                c.execute("UPDATE pm_jobs SET status='stale',input_json='{}' WHERE project=? AND session_id=? AND status IN ('running','failed') AND id!=?", (project, lane[0], job_id))
                c.execute("INSERT INTO pm_jobs(id,project,session_id,fingerprint,token,status,lease_until,attempts,revision,generation,input_json) VALUES (?,?,?,?,?,'running',?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET token=excluded.token,status='running',lease_until=excluded.lease_until,attempts=excluded.attempts,input_json=excluded.input_json,error=NULL", (job_id, project, lane[0], fingerprint, token, now+max(1,min(int(lease_seconds),3600)), (prior[0]+1) if prior else 1, state["revision"], state["deletion_generation"], _json(snapshot)))
                return snapshot
            return self.store._write(claim_job)

    def _disclose_history(self, project: str, state: dict, sources: list[dict], max_chars: int, max_sources: int) -> dict:
        """Supply original evidence for retained fields, within the existing input budget."""
        c = self.store._connection
        fresh_ids = {value.casefold() for source in sources if source["source_role"] == "user_intent"
                     for value in re.findall(TASK_ID_PATTERN, source["body"])}
        records = [item for item in state["episodes"] if fresh_ids &
                   {value.casefold() for value in re.findall(TASK_ID_PATTERN, item.get("goal", ""))}]
        omitted_heads = max(0, len(records)-4)
        records = records[:4]
        seen = {item["id"] for item in records}
        omitted_ancestors = 0
        for head in list(records):
            parent = head.get("parent_episode_id")
            for _ in range(8):
                if not parent or parent in seen:
                    break
                row = c.execute("SELECT data_json FROM pm_objects WHERE project=? AND id=?", (project, parent)).fetchone()
                if not row:
                    break
                record = json.loads(row[0])
                records.append(record)
                seen.add(parent)
                parent = record.get("parent_episode_id")
            if parent and parent not in seen:
                omitted_ancestors += 1
        ids = list(dict.fromkeys(ref["source_id"] for item in records for refs in item.get("evidence", {}).values() for ref in refs))
        included = {source["id"] for source in sources}
        remaining = max_chars - sum(len(source["body"]) for source in sources)
        disclosed = omitted = 0
        for source_id in ids:
            if source_id in included:
                continue
            row = c.execute("SELECT e.*,v.seq FROM entries e JOIN pm_events v ON v.source_id=e.id WHERE e.project=? AND e.id=? AND v.done=1 AND length(e.body)<=?", (project, source_id, remaining)).fetchone() if len(sources) < max_sources else None
            if row is None:
                omitted += 1
                continue
            sources.append({key: row[key] for key in ("id", "title", "source", "session_id", "turn_id", "created_at", "seq")} |
                {"body": row["body"], "body_offset": 0, "body_length": len(row["body"]), "source_role": _role(row["source"]), "truncated": False, "historical": True})
            included.add(source_id)
            remaining -= len(row["body"])
            disclosed += 1
        return {"disclosed_sources": disclosed, "omitted_sources": omitted,
                "omitted_ancestor_chains": omitted_ancestors, "omitted_heads": omitted_heads, "complete_history": False,
                "source_char_limit": max_chars, "source_limit": max_sources}

    def _reconsideration(self, project: str, *, include_completed: bool = False, discovery: dict | None = None, max_sources: int = 32, max_chars: int = 24000) -> dict[str, Any] | None:
        """Discover a bounded pair of split heads; reads never call a model."""
        c = self.store._connection
        state = self._state(project, 100, 0, include_project_obligations=True, include_coverage=False)
        groups: dict[str, list[dict]] = {}
        contexts: dict[str, str] = {}
        inspected = set()
        identity_chars = 0
        discovery = discovery if discovery is not None else {}
        discovery.update(identity_source_limit=64, identity_char_limit=24000,
                         identity_sources_omitted=0, ambiguous_heads=0)
        for episode in state["episodes"]:
            for field in ("goal", "summary"):
                for ref in episode.get("evidence", {}).get(field, []):
                    source_id = ref["source_id"]
                    if ref.get("source_role") != "user_intent" or source_id in inspected:
                        continue
                    if len(inspected) >= 64:
                        discovery["identity_sources_omitted"] += 1
                        continue
                    inspected.add(source_id)
                    row = c.execute("SELECT body FROM entries WHERE project=? AND id=? AND length(body)<=?", (project, source_id, 24000-identity_chars)).fetchone()
                    if row:
                        contexts[source_id] = row[0]
                        identity_chars += len(row[0])
                    else:
                        discovery["identity_sources_omitted"] += 1
            identity = _source_task_identity(episode, contexts)
            if identity:
                groups.setdefault(identity, []).append(episode)
            elif re.search(TASK_ID_PATTERN, episode.get("goal", "")):
                discovery["ambiguous_heads"] += 1
        for identity, heads in sorted(groups.items()):
            if len(heads) < 2:
                continue
            all_heads = sorted(heads, key=lambda item: (item["revision"], item["id"]))
            heads = all_heads if len(all_heads) <= 4 else [*all_heads[:3], all_heads[-1]]
            fingerprint = _hash([RECONSIDERATION_VERSION, project, state["deletion_generation"], max_sources, max_chars, [item["id"] for item in all_heads]])
            job_id = "pmj_" + fingerprint[:40]
            job = c.execute("SELECT status,lease_until,retry_after FROM pm_jobs WHERE id=?", (job_id,)).fetchone()
            if not include_completed and job and (job["status"] in {"completed", "quarantined"} or
                        job["status"] == "running" and job["lease_until"] > time.time() or
                        job["status"] == "failed" and job["retry_after"] > time.time()):
                continue
            records = list(heads)
            # Include bounded ancestors so an old goal or reason can survive a
            # newer head that only cited its last delta. No transcript replay.
            seen = {item["id"] for item in records}
            omitted_ancestors = 0
            for head in heads:
                parent = head.get("parent_episode_id")
                for _ in range(8):
                    if not parent or parent in seen:
                        break
                    row = c.execute("SELECT data_json FROM pm_objects WHERE project=? AND id=?", (project, parent)).fetchone()
                    if not row:
                        break
                    record = json.loads(row[0])
                    records.append(record)
                    seen.add(parent)
                    parent = record.get("parent_episode_id")
                if parent and parent not in seen:
                    omitted_ancestors += 1
            claims = [item for item in state["claims"] if identity in {value.casefold() for value in re.findall(TASK_ID_PATTERN, item.get("text", ""))}]
            records.extend(claims)
            source_ids = list(dict.fromkeys(ref["source_id"] for item in records for refs in item.get("evidence", {}).values() for ref in refs))
            sources = []
            remaining = max_chars
            omitted = 0
            for source_id in source_ids:
                row = c.execute("SELECT e.*,v.seq FROM entries e JOIN pm_events v ON v.source_id=e.id WHERE e.project=? AND e.id=? AND v.done=1 AND length(e.body)<=?", (project, source_id, remaining)).fetchone() if len(sources) < max_sources else None
                if row is None:
                    omitted += 1
                    continue
                sources.append({key: row[key] for key in ("id", "title", "source", "session_id", "turn_id", "created_at", "seq")} |
                    {"body": row["body"], "body_offset": 0, "body_length": len(row["body"]), "source_role": _role(row["source"]), "truncated": False, "historical": True})
                remaining -= len(row["body"])
            if not sources:
                continue
            bounded = _bounded_generation_state({**state, "episodes": heads}, sources)
            return {"job_id": job_id, "project": project, "session_id": "__pm_reconsideration__",
                    "revision": state["revision"], "deletion_generation": state["deletion_generation"],
                    "input_fingerprint": fingerprint, "processor_version": PROCESSOR_VERSION,
                    "job_kind": "reconsideration", "sources": sorted(sources, key=lambda item: item["seq"]), "state": bounded,
                    "reconsideration": {"task_id": identity, "candidate_head_ids": [item["id"] for item in heads],
                        "candidate_window_bounded": True, "head_limit": 100, "ancestor_limit_per_head": 8,
                        "omitted_heads": len(all_heads)-len(heads),
                        "source_limit": max_sources, "source_char_limit": max_chars, "omitted_sources": omitted,
                        "omitted_ancestor_chains": omitted_ancestors,
                        "complete_history": False, **discovery}}
        return None

    def _claim_reconsideration(self, project: str, now: float, lease_seconds: int, max_sources: int, max_chars: int) -> dict[str, Any] | None:
        snapshot = self._reconsideration(project, max_sources=min(max_sources, 32), max_chars=min(max_chars, 24000))
        if snapshot is None:
            return None
        c = self.store._connection
        prior = c.execute("SELECT attempts FROM pm_jobs WHERE id=?", (snapshot["job_id"],)).fetchone()
        snapshot["token"] = uuid.uuid4().hex
        c.execute("INSERT INTO pm_jobs(id,project,session_id,fingerprint,token,status,lease_until,attempts,revision,generation,input_json) VALUES (?,?,?,?,?,'running',?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET token=excluded.token,status='running',lease_until=excluded.lease_until,attempts=excluded.attempts,revision=excluded.revision,generation=excluded.generation,input_json=excluded.input_json,error=NULL",
                  (snapshot["job_id"], project, snapshot["session_id"], snapshot["input_fingerprint"], snapshot["token"], now+max(1,min(int(lease_seconds),3600)), prior[0]+1 if prior else 1, snapshot["revision"], snapshot["deletion_generation"], _json(snapshot)))
        return snapshot

    def _job(self, supplied: dict[str, Any]) -> tuple[Any, Any]:
        c = self.store._connection
        job = c.execute("SELECT * FROM pm_jobs WHERE id=? AND project=?", (supplied.get("job_id"), supplied.get("project"))).fetchone()
        project = c.execute("SELECT * FROM pm_projects WHERE project=?", (supplied.get("project"),)).fetchone()
        return job, project

    @staticmethod
    def _live(job: Any, project: Any, supplied: dict[str, Any]) -> bool:
        return bool(job and project and job["status"] == "running" and job["token"] == supplied.get("token") and job["fingerprint"] == supplied.get("input_fingerprint") and job["lease_until"] > time.time() and job["revision"] == project["revision"] and job["generation"] == project["deletion_generation"])

    def begin_attempt(self, snapshot: dict[str, Any], *, model: str, reasoning_effort: str,
                      processor_id: str = "", attempt_id: str | None = None, profile=None) -> dict[str, Any]:
        """Record one native invocation before dispatch, independently of its job.

        A caller can reuse an explicit attempt ID after an uncertain write. A
        new invocation, including a retry of one job, must receive a new ID.
        No prompts, source text, or generated text enter this ledger.
        """
        from .execution import normalize_profile
        requested = normalize_profile(profile.to_dict()) if profile is not None else None
        if requested is not None:
            model, reasoning_effort = requested["model"], requested["reasoning_effort"] or "none"
        else:
            model = _attempt_identifier(model, "model")
        processor_id = _attempt_identifier(processor_id, "processor_id", empty=True)
        if reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            raise ValueError("unsupported attempt reasoning effort")
        identity = _attempt_identifier(attempt_id or "pma_"+uuid.uuid4().hex, "attempt_id")
        values = requested or dict(runtime="codex", provider="openai", model=model,
                                   reasoning_effort=reasoning_effort, timeout=None)
        with self.store._lock:
            def begin():
                connection = self.store._connection
                job, project = self._job(snapshot)
                if not self._live(job, project, snapshot):
                    raise ValueError("attempt requires a live claimed job")
                old = connection.execute("SELECT * FROM pm_usage_attempts WHERE attempt_id=?", (identity,)).fetchone()
                if old:
                    expected = (snapshot.get("project"), snapshot.get("job_id"), snapshot.get("token"), model, reasoning_effort, processor_id)
                    actual = tuple(old[key] for key in ("project", "job_id", "claim_token", "model", "reasoning_effort", "processor_id"))
                    if expected != actual or any(old["requested_" + key] != value for key, value in values.items()):
                        raise ValueError("attempt ID already belongs to another invocation")
                    return dict(old)
                now = _attempt_now()
                connection.execute("INSERT INTO pm_usage_attempts(attempt_id,project,session_id,job_id,claim_token,processor_id,model,reasoning_effort,started_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (identity, job["project"], job["session_id"], job["id"], job["token"], processor_id, model, reasoning_effort, now, now))
                connection.execute("UPDATE pm_usage_attempts SET requested_runtime=?,requested_provider=?,requested_model=?,requested_reasoning_effort=?,requested_timeout=?,profile_basis=? WHERE attempt_id=?",
                    (values["runtime"], values["provider"], values["model"], values["reasoning_effort"], values["timeout"],
                     "attempt_profile" if requested else "legacy_requested_profile", identity))
                return dict(connection.execute("SELECT * FROM pm_usage_attempts WHERE attempt_id=?", (identity,)).fetchone())
            return self.store._write(begin)

    def checkpoint_attempt(self, project: Any, attempt_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        """Merge cumulative usage without requiring the source/job to survive."""
        with self.store._lock:
            return self.store._write(lambda: self._update_attempt(self._project(project), attempt_id, receipt, None))

    def finalize_attempt(self, project: Any, attempt_id: str, outcome: str,
                         receipt: dict[str, Any] | None = None) -> dict[str, Any]:
        """Finalize once; later telemetry can improve totals, not rewrite outcome."""
        if outcome not in {"processed", "completed", "committed", "failed", "deferred", "stale", "interrupted", "cancelled", "disabled", "unknown"}:
            raise ValueError("unsupported attempt outcome")
        with self.store._lock:
            return self.store._write(lambda: self._update_attempt(self._project(project), attempt_id, receipt or {}, outcome))

    def _update_attempt(self, project: str, attempt_id: str, receipt: dict[str, Any], outcome: str | None) -> dict[str, Any]:
        connection = self.store._connection
        old_row = connection.execute("SELECT * FROM pm_usage_attempts WHERE project=? AND attempt_id=?", (project, attempt_id)).fetchone()
        if old_row is None:
            raise ValueError("attempt does not exist in this project")
        old = dict(old_row)
        observed = _attempt_metrics(receipt)
        updated = dict(old)
        identity = receipt.get("identity")
        if isinstance(identity, dict):
            fields = {"runtime": "runtime", "provider": "provider", "model": "actual_model",
                      "reasoning_effort": "actual_reasoning_effort"}
            if set(identity) != set(fields) or any(identity[key] != old["requested_" + key] for key in fields):
                raise ValueError("execution identity does not match requested profile")
            for key, field in fields.items():
                if old[field] is not None and old[field] != identity[key]:
                    raise ValueError("execution identity changed")
                updated[field] = identity[key]
        token_changed = False
        for field in _ATTEMPT_COUNTERS:
            incoming = observed.get(field)
            if incoming is not None and (old[field] is None or incoming > old[field]):
                updated[field] = incoming
                token_changed = True
        for field in ("worker_thread_id", "worker_turn_id"):
            if observed.get(field) and (old[field] is None or field == "worker_turn_id"):
                updated[field] = observed[field]
        if observed.get("duration_ms") is not None:
            updated["duration_ms"] = max(old["duration_ms"] or 0, observed["duration_ms"])
        ranks = {"unknown": 0, "partial": 1, "reported": 2}
        if ranks[observed["usage_status"]] > ranks[old["usage_status"]]:
            updated["usage_status"] = observed["usage_status"]
        if observed["usage_source"] != "unknown":
            updated["usage_source"] = observed["usage_source"]
        updated["usage_updates"] = max(old["usage_updates"] + int(token_changed), observed["usage_updates"])
        if old["error_code"] is None and observed.get("error_code"):
            updated["error_code"] = observed["error_code"]
        if outcome is not None and old["finished_at"] is None:
            updated.update(outcome=outcome, finished_at=_attempt_now())
        changed = {key: value for key, value in updated.items() if value != old[key]}
        if changed:
            changed["updated_at"] = _attempt_now()
            connection.execute("UPDATE pm_usage_attempts SET "+",".join(key+"=?" for key in changed)+" WHERE project=? AND attempt_id=?", (*changed.values(), project, attempt_id))
        return dict(connection.execute("SELECT * FROM pm_usage_attempts WHERE project=? AND attempt_id=?", (project, attempt_id)).fetchone())

    def attempts(self, project: Any, limit: int = 100) -> list[dict[str, Any]]:
        with self.store._lock:
            return self.store._read(lambda: [dict(row) for row in self.store._connection.execute("SELECT * FROM pm_usage_attempts WHERE project=? ORDER BY started_at DESC,attempt_id DESC LIMIT ?", (self._project(project), max(1, min(int(limit), 1000))))])

    def snapshot_current(self, snapshot: dict[str, Any]) -> bool:
        with self.store._lock:
            return self.store._read(lambda: self._live(*self._job(snapshot), snapshot))

    def fail(self, snapshot: dict[str, Any], reason: str, *, receipt: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.store._lock:
            def fail_job():
                job, project = self._job(snapshot)
                if not self._live(job, project, snapshot):
                    if job and job["token"] == snapshot.get("token") and job["status"] == "running":
                        self.store._connection.execute("UPDATE pm_jobs SET status='stale',input_json='{}' WHERE id=?", (job["id"],))
                    return {"status": "stale"}
                status = "quarantined" if reason != "configuration_error" and (reason == "invalid_response" or job["attempts"] >= 5) else "failed"
                # Do not persist arbitrary model/runner errors: they may contain
                # sensitive inputs. Only a bounded error category is durable.
                category = reason if reason in {"configuration_error", "invalid_response", "timeout", "runner_failure", "unavailable"} else "processing_failed"
                retry_after = time.time()+min(300, 2**job["attempts"])
                if status == "quarantined":
                    durable = json.loads(job["input_json"])
                    for source in durable.get("sources", []):
                        self.store._connection.execute("UPDATE pm_events SET blocked_job_id=? WHERE project=? AND source_id=? AND done=0", (job["id"], job["project"], source["id"]))
                self.store._connection.execute("UPDATE pm_jobs SET status=?,error=?,retry_after=?,input_json='{}',receipt_json=? WHERE id=?", (status, category, retry_after, _json(_safe_receipt(receipt)), job["id"]))
                return {"status": status, "job_id": job["id"], "attempts": job["attempts"], "retry_after": retry_after}
            return self.store._write(fail_job)

    def retry_failed(self, project: Any, include_quarantined: bool = False) -> int:
        with self.store._lock:
            def retry():
                statuses = "('failed','quarantined')" if include_quarantined else "('failed')"
                self.store._connection.execute(f"UPDATE pm_events SET blocked_job_id=NULL WHERE project=? AND blocked_job_id IN (SELECT id FROM pm_jobs WHERE project=? AND status IN {statuses})", (self._project(project), self._project(project)))
                return self.store._connection.execute(f"UPDATE pm_jobs SET status='stale',retry_after=0,attempts=0 WHERE project=? AND status IN {statuses}", (self._project(project),)).rowcount
            return self.store._write(retry)

    def _validate(self, snapshot: dict[str, Any], output: dict[str, Any], *, conservative_transitions: bool = False) -> tuple[list[dict], list[dict], dict[str, Any]]:
        if not isinstance(output, dict) or set(output)-{"episodes", "claims", "relations"}:
            raise ValueError("project memory output must contain episodes, claims and relations only")
        if len(_json(output)) > 180000:
            raise ValueError("project memory output exceeds limit")
        sources = {s["id"]: s for s in snapshot["sources"]}
        contexts = {}
        for source_id in sources:
            row = self.store._connection.execute("SELECT body FROM entries WHERE project=? AND id=?", (snapshot["project"], source_id)).fetchone()
            if row:
                contexts[source_id] = row[0]
        def spans(value: Any) -> list[dict]:
            if not isinstance(value, list) or not value or len(value)>32:
                raise ValueError("each field needs bounded exact source evidence")
            result = []
            for ref in value:
                if not isinstance(ref, dict) or set(ref)-{"source_id", "start", "end", "quote"}:
                    raise ValueError("invalid evidence reference")
                source = sources.get(ref.get("source_id"))
                start, end, quote = ref.get("start"), ref.get("end"), ref.get("quote")
                if source is None or type(start) is not int or type(end) is not int or not isinstance(quote, str) or not quote or not 0<=start<end<=len(source["body"]) or source["body"][start:end] != quote:
                    raise ValueError("evidence must quote an exact available source span")
                result.append(dict(ref, start=start+source["body_offset"], end=end+source["body_offset"], source_role=source["source_role"], session_id=source["session_id"], source_recorded_at=source["created_at"], event_at=None, event_time_basis="source_capture_time"))
            return result
        objects: list[dict] = []
        downgraded_reasons: list[str] = []
        omitted_continuities: list[str] = []
        continued_roots: set[str] = set()
        keys: dict[str, str] = {}
        for plural, kind, fields in (("episodes", "episode", ("summary", "goal", "rationale", "outcome", "open_work")), ("claims", "claim", ("text", "rationale"))):
            items = output.get(plural, [])
            if not isinstance(items, list) or len(items)>64:
                raise ValueError("too many project memory objects")
            for item in items:
                allowed = {"key", "evidence", *fields} | ({"kind", "workflow_state", "scope"} if kind == "claim" else {"continuation_of", "consolidation_of"})
                if not isinstance(item, dict) or set(item)-allowed:
                    raise ValueError("unsupported object fields; verification cannot be self-assigned")
                key = item.get("key")
                if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", key) or key in keys:
                    raise ValueError("object keys must be unique bounded identifiers")
                if not isinstance(item.get(fields[0]), str) or not item[fields[0]].strip():
                    raise ValueError("object text is required")
                evidence = item.get("evidence")
                if not isinstance(evidence, dict) or set(evidence)-set(fields)-({"workflow_state"} if kind == "claim" else {"continuation_of", "consolidation_of"}):
                    raise ValueError("invalid field evidence")
                clean = {"key": key, "object_type": kind}
                clean_evidence = {}
                for field in fields:
                    value = item.get(field)
                    if value is not None:
                        if not isinstance(value, str) or len(value)>6000:
                            raise ValueError("prose fields must be bounded strings")
                        clean[field] = value
                        if value:
                            clean_evidence[field] = spans(evidence.get(field))
                if kind == "claim":
                    state = item.get("workflow_state", "proposed")
                    if state not in STATES:
                        raise ValueError("unsupported workflow state")
                    scope = item.get("scope", {})
                    if not isinstance(scope, dict) or set(scope)-{"environment", "version", "worktree"} or any(not isinstance(v, str) or len(v)>500 for v in scope.values()):
                        raise ValueError("invalid claim scope")
                    if scope.get("worktree") and scope["worktree"] != snapshot["project"]:
                        raise ValueError("claim worktree must match project")
                    claim_kind = item.get("kind", "fact")
                    if claim_kind not in {"decision", "constraint", "requirement", "task", "result", "question", "fact", "goal", "correction"}:
                        raise ValueError("unsupported claim kind")
                    clean.update(kind=claim_kind, workflow_state=state, scope={"environment": "", "version": "", "worktree": snapshot["project"]} | scope)
                    clean_evidence["workflow_state"] = spans(evidence.get("workflow_state", evidence.get("text")))
                    state_error = None
                    if state == "completed":
                        if not _explicit_transition(clean_evidence["workflow_state"], "closes", contexts, subject=clean["text"]):
                            state_error = ("unproved_completion", "completion needs explicit source evidence with the same subject directly stated")
                        elif not any(_normalized_phrase(clean["text"]) == _normalized_phrase(ref["quote"]) for ref in clean_evidence["workflow_state"]):
                            state_error = ("unproved_completion", "completed claim must be directly stated by its evidence")
                    if state == "accepted" and not any(ref["source_role"] == "user_intent" and _accepted_evidence(ref, contexts.get(ref["source_id"], "")) for ref in clean_evidence["workflow_state"]):
                        state_error = ("unproved_acceptance", "accepted decisions need explicit user authority")
                    if state_error:
                        if not conservative_transitions:
                            raise ValueError(state_error[1])
                        clean.update(workflow_state="proposed", requested_state=state)
                        downgraded_reasons.append(state_error[0])
                    # Model-produced paraphrases retain inferred provenance even
                    # with valid quotations. Exact text can carry its raw role.
                    direct = [r for r in clean_evidence["text"] if r["quote"] == clean["text"]]
                    clean["source_role"] = direct[0]["source_role"] if direct else "model_inference"
                    clean["evidence_basis"] = "exact_quote" if direct else "model_inference"
                    if state_error:
                        clean.update(source_role="model_inference", evidence_basis="model_inference")
                clean["evidence"] = clean_evidence
                clean["id"] = "pm_" + _hash([snapshot["job_id"], kind, key])[:40]
                clean.update(project=snapshot["project"], revision=snapshot["revision"]+1, session_id=snapshot["session_id"])
                if kind == "episode":
                    clean.update(logical_episode_id=clean["id"], episode_version=1, parent_episode_id=None, continuity_basis="standalone")
                    target_id = item.get("continuation_of")
                    continuation_refs = evidence.get("continuation_of")
                    if target_id is None:
                        if continuation_refs not in (None, []):
                            raise ValueError("continuation evidence requires an explicit prior head")
                    else:
                        if not isinstance(target_id, str):
                            raise ValueError("continuation_of must identify a known project episode")
                        prior_row = self.store._connection.execute("SELECT data_json FROM pm_objects WHERE project=? AND id=? AND object_type='episode'", (snapshot["project"], target_id)).fetchone()
                        if prior_row is None:
                            raise ValueError("continuation_of must identify a known project episode")
                        prior = json.loads(prior_row[0])
                        _episode_identity(self.store._connection, snapshot["project"], prior)
                        refs = spans(continuation_refs) if continuation_refs not in (None, []) else []
                        if refs:
                            clean_evidence["continuation_of"] = refs
                        continuity_contexts = dict(contexts)
                        for source_id in {ref["source_id"] for field in ("goal", "summary") for ref in prior.get("evidence", {}).get(field, [])}:
                            retained = self.store._connection.execute("SELECT body FROM entries WHERE project=? AND id=?", (snapshot["project"], source_id)).fetchone()
                            if retained is not None:
                                continuity_contexts[source_id] = retained[0]
                        continuity_refs = refs if snapshot.get("job_kind") == "reconsideration" else [ref for ref in refs if not sources[ref["source_id"]].get("historical")]
                        continuity_target = prior
                        if snapshot.get("job_kind") == "reconsideration":
                            identity = _source_task_identity(prior, contexts)
                            continuity_target = {**prior, "goal": identity or ""}
                        if not continuity_refs or not _continuity_supported(continuity_refs, continuity_target, continuity_contexts):
                            omitted_continuities.append("unproved_continuity")
                        else:
                            if prior["logical_episode_id"] in continued_roots:
                                raise ValueError("one continuation per logical episode is allowed in each job")
                            continued_roots.add(prior["logical_episode_id"])
                            clean.update(logical_episode_id=prior["logical_episode_id"], episode_version=prior["episode_version"]+1, parent_episode_id=prior["id"], continuity_basis="model_inference")
                if kind == "episode" and snapshot.get("job_kind") == "reconsideration" and not item.get("consolidation_of"):
                    raise ValueError("reconsideration episodes must reduce the number of split heads")
                if kind == "episode" and item.get("consolidation_of"):
                    targets = item["consolidation_of"]
                    if snapshot.get("job_kind") != "reconsideration" or not isinstance(targets, list) or len(targets) > 3 or len(set(targets)) != len(targets):
                        raise ValueError("consolidation needs a bounded reconsideration job")
                    if not clean["parent_episode_id"] or clean["parent_episode_id"] in targets:
                        raise ValueError("consolidation needs a distinct continued head")
                    refs = spans(evidence.get("consolidation_of"))
                    clean_evidence["consolidation_of"] = refs
                    available = {record["id"]: record for record in snapshot["state"]["episodes"]}
                    if snapshot["reconsideration"]["omitted_sources"] or snapshot["reconsideration"].get("omitted_ancestor_chains"):
                        raise ValueError("consolidation cannot hide undisclosed source evidence")
                    identity = _source_task_identity(available[clean["parent_episode_id"]], contexts)
                    for prior_id in [clean["parent_episode_id"], *targets]:
                        prior = available.get(prior_id)
                        if prior is None:
                            raise ValueError("unknown consolidation head")
                        for field in ("goal", "rationale", "outcome", "open_work"):
                            if prior.get(field) and not clean.get(field):
                                raise ValueError("consolidation must retain supported task fields")
                    for target in targets:
                        prior = available.get(target)
                        if prior is None:
                            raise ValueError("unknown consolidation head")
                        if not identity or _source_task_identity(prior, contexts) != identity or not _continuity_supported(refs, {**prior, "goal": identity}, contexts):
                            raise ValueError("consolidation requires original task identity and explicit continuity")
                        if prior["logical_episode_id"] in continued_roots:
                            raise ValueError("episode is already consolidated in this job")
                        continued_roots.add(prior["logical_episode_id"])
                    clean["consolidated_episode_ids"] = targets
                for field in fields if kind == "episode" else ("rationale",):
                    if clean.get(field):
                        text, field_refs, provenance = _grounded_field(clean_evidence[field], sources)
                        clean[field] = text
                        clean_evidence[field] = field_refs
                        clean.setdefault("field_provenance", {})[field] = provenance
                keys[key] = clean["id"]
                objects.append(clean)
        relations = output.get("relations", [])
        if not isinstance(relations, list) or len(relations)>128:
            raise ValueError("too many relations")
        known = {o["id"]: o for o in objects}
        known.update({o["id"]: o for o in snapshot["state"]["claims"]+snapshot["state"]["episodes"]})
        clean_relations = []
        omitted_reasons = []
        for item in relations:
            if not isinstance(item, dict) or set(item)-{"type", "from", "to", "evidence"} or item.get("type") not in RELATIONS:
                raise ValueError("invalid relation")
            from_id, to_id = keys.get(item.get("from"), item.get("from")), keys.get(item.get("to"), item.get("to"))
            if from_id not in known or to_id not in known or from_id == to_id:
                raise ValueError("relations must connect distinct known project objects")
            evidence = spans(item.get("evidence"))
            if item["type"] in {"closes", "replaces"}:
                origin, target = known[from_id], known[to_id]
                domain_error = None
                if origin["object_type"] != "claim" or target["object_type"] != "claim" or origin.get("scope") != target.get("scope"):
                    domain_error = ("scope_mismatch", "state transitions require identical claim scope")
                elif origin.get("requested_state"):
                    domain_error = ("unproved_origin_state", "a proposed unproved state cannot close or replace existing work")
                elif not _explicit_transition(evidence, item["type"], contexts, subject=target["text"]):
                    domain_error = ("unproved_transition", "transition evidence must name the full target subject in an explicit affirmative source sentence")
                if domain_error:
                    if conservative_transitions:
                        # Only a trusted caller can opt into retaining valid
                        # proposals. IDs and raw spans were checked above;
                        # omitted transitions never change either claim.
                        omitted_reasons.append(domain_error[0])
                        continue
                    raise ValueError(domain_error[1])
            clean = {"type": item["type"], "from": from_id, "to": to_id, "evidence": evidence}
            clean["id"] = "pmr_" + _hash([snapshot["job_id"], clean])[:40]
            clean_relations.append(clean)
        return objects, clean_relations, {"omitted_transitions_count": len(omitted_reasons), "omitted_transitions_reason": ",".join(sorted(set(omitted_reasons))), "downgraded_states_count": len(downgraded_reasons), "downgraded_states_reason": ",".join(sorted(set(downgraded_reasons))), "omitted_continuities_count": len(omitted_continuities), "omitted_continuities_reason": ",".join(sorted(set(omitted_continuities))), "pending_reconciliation": bool(omitted_reasons or downgraded_reasons or omitted_continuities)}

    def _prepare_records(self, durable: dict[str, Any], output: dict[str, Any], *,
                         conservative_transitions: bool) -> tuple[list[dict], list[dict], dict[str, Any]]:
        c = self.store._connection
        objects, relations, reconciliation = self._validate(durable, output, conservative_transitions=conservative_transitions)
        # Preserve superseded narrative as explicitly historical context.
        # It cannot reopen or close independently scoped claims. This
        # avoids turning a compound old paragraph into a new obligation.
        for episode in objects:
            if episode["object_type"] != "episode":
                continue
            prior_ids = [episode.get("parent_episode_id"), *episode.get("consolidated_episode_ids", [])]
            priors = []
            for prior_id in filter(None, prior_ids):
                row = c.execute("SELECT data_json FROM pm_objects WHERE project=? AND id=?", (durable["project"], prior_id)).fetchone()
                if row:
                    priors.append(json.loads(row[0]))
            omitted_context = sum(prior.get("retained_context_fields_omitted", 0) for prior in priors)
            disclosed = {source["id"]: source for source in durable["sources"]}
            for field in ("goal", "rationale", "outcome", "open_work"):
                historical = "prior_" + field
                texts, refs = [], []
                partial_context = False
                for prior in priors:
                    for name in (historical, field):
                        text = prior.get(name)
                        evidence = prior.get("evidence", {}).get(name, [])
                        if not text or not evidence:
                            continue
                        try:
                            text, evidence, provenance = _grounded_field(evidence, disclosed)
                        except ValueError:
                            omitted_context += 1
                            continue
                        if text == episode.get(field) or text in texts:
                            continue
                        combined = list(refs)
                        combined.extend(ref for ref in evidence if ref not in combined)
                        if (len("\n".join([*texts, text])) > 6000 or len(combined) > 32
                                or sum(len(ref["quote"]) for ref in combined) > 6000):
                            omitted_context += 1
                            continue
                        texts.append(text)
                        refs = combined
                        partial_context = partial_context or provenance["partial_context"]
                if texts:
                    episode[historical] = "\n".join(texts)
                    episode["evidence"][historical] = refs
                    episode.setdefault("field_provenance", {})[historical] = {
                        "basis": "attributed_source_excerpts", "field_association": "model_inference",
                        "source_roles": list(dict.fromkeys(ref["source_role"] for ref in refs)),
                        "partial_context": partial_context}
            if any(episode.get("prior_" + field) for field in ("goal", "rationale", "outcome", "open_work")):
                episode["prior_context_status"] = "historical_current_validity_unresolved"
                reconciliation["pending_reconciliation"] = True
            episode["retained_context_fields_omitted"] = omitted_context
        if len(_json({"objects": objects, "relations": relations})) > 180000:
            raise ValueError("assembled project memory output exceeds limit")
        return objects, relations, reconciliation

    def prepare(self, snapshot: dict[str, Any], output: dict[str, Any], *,
                conservative_transitions: bool = False) -> dict[str, Any]:
        """Read final records for optional assessment outside the write transaction.

        Commit rebuilds the records and compares their digest. This preview does
        not grant a lease or bypass revision, source, deletion or head checks.
        """
        if type(conservative_transitions) is not bool:
            raise ValueError("conservative_transitions must be a boolean trusted caller option")
        with self.store._lock:
            def read():
                job, project = self._job(snapshot)
                if not self._live(job, project, snapshot):
                    raise ValueError("preparation requires a live claimed job")
                durable = json.loads(job["input_json"])
                for source in durable["sources"]:
                    current = self.store._connection.execute(
                        "SELECT body FROM entries WHERE project=? AND id=?",
                        (durable["project"], source["id"])).fetchone()
                    if current is None or current[0][source["body_offset"]:source["body_offset"]+len(source["body"])] != source["body"]:
                        raise ValueError("preparation source changed")
                objects, relations, reconciliation = self._prepare_records(
                    durable, output, conservative_transitions=conservative_transitions)
                prepared = {"objects": objects, "relations": relations, "reconciliation": reconciliation}
                return dict(prepared, digest=_hash(prepared))
            return self.store._read(read)

    def commit(self, snapshot: dict[str, Any], output: dict[str, Any], *, receipt: dict[str, Any] | None = None, conservative_transitions: bool = False, expected_prepared_digest: str | None = None) -> dict[str, Any]:
        with self.store._lock:
            def commit_job():
                c = self.store._connection
                job, project = self._job(snapshot)
                if job and job["status"] == "completed" and job["token"] == snapshot.get("token"):
                    return dict(json.loads(job["receipt_json"]), status="duplicate")
                if not self._live(job, project, snapshot):
                    if job and job["token"] == snapshot.get("token") and job["status"] == "running":
                        c.execute("UPDATE pm_jobs SET status='stale',input_json='{}' WHERE id=?", (job["id"],))
                    return {"status": "stale"}
                durable = json.loads(job["input_json"])
                # Never validate against caller-mutated sources or state.
                for source in durable["sources"]:
                    current = c.execute("SELECT body FROM entries WHERE id=? AND project=?", (source["id"], durable["project"])).fetchone()
                    if current is None or current[0][source["body_offset"]:source["body_offset"]+len(source["body"])] != source["body"]:
                        c.execute("UPDATE pm_jobs SET status='stale',input_json='{}' WHERE id=?", (job["id"],))
                        return {"status": "stale"}
                if isinstance(output, dict) and isinstance(output.get("episodes", []), list):
                    for proposed in output.get("episodes", []):
                        target_id = proposed.get("continuation_of") if isinstance(proposed, dict) else None
                        if isinstance(target_id, str):
                            target = c.execute("SELECT data_json FROM pm_objects WHERE project=? AND id=? AND object_type='episode'", (durable["project"], target_id)).fetchone()
                            if target:
                                prior = json.loads(target[0])
                                _episode_identity(c, durable["project"], prior)
                                if not prior["is_current_head"]:
                                    c.execute("UPDATE pm_jobs SET status='stale',input_json='{}' WHERE id=?", (job["id"],))
                                    return {"status": "stale", "reason": "episode_head_changed"}
                if type(conservative_transitions) is not bool:
                    raise ValueError("conservative_transitions must be a boolean trusted caller option")
                objects, relations, reconciliation = self._prepare_records(
                    durable, output, conservative_transitions=conservative_transitions)
                if expected_prepared_digest is not None and expected_prepared_digest != _hash(
                        {"objects": objects, "relations": relations, "reconciliation": reconciliation}):
                    raise ValueError("prepared project memory changed after assessment")
                derived_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                for obj in objects:
                    obj.update(created_at=derived_at, updated_at=derived_at, derived_created_at=derived_at, event_at=None, event_time_basis="source_capture_time")
                    c.execute("INSERT INTO pm_objects VALUES (?,?,?,?,?,?)", (obj["id"], durable["project"], obj["object_type"], durable["session_id"], durable["revision"]+1, _json(obj)))
                    refs = {r["source_id"] for values in obj["evidence"].values() for r in values}
                    c.executemany("INSERT INTO pm_evidence VALUES (?,?)", [(obj["id"], source_id) for source_id in refs])
                    if obj["object_type"] == "episode":
                        logical = obj["logical_episode_id"]
                        parent = obj["parent_episode_id"]
                        if parent:
                            # Legacy roots are adopted only when explicitly
                            # continued. No full-history migration is needed.
                            c.execute("INSERT OR IGNORE INTO pm_episode_heads VALUES (?,?,?,?)", (durable["project"], logical, parent, obj["episode_version"]-1))
                            changed = c.execute("UPDATE pm_episode_heads SET head_id=?,episode_version=? WHERE project=? AND logical_episode_id=? AND head_id=?", (obj["id"], obj["episode_version"], durable["project"], logical, parent)).rowcount
                            if changed != 1:
                                raise ValueError("episode head changed during atomic commit")
                        else:
                            c.execute("INSERT INTO pm_episode_heads VALUES (?,?,?,?)", (durable["project"], logical, obj["id"], 1))
                for obj in objects:
                    for target_id in obj.get("consolidated_episode_ids", []):
                        target = next(item for item in durable["state"]["episodes"] if item["id"] == target_id)
                        logical = target["logical_episode_id"]
                        c.execute("INSERT OR IGNORE INTO pm_episode_heads VALUES (?,?,?,?)", (durable["project"], logical, target_id, target["episode_version"]))
                        if c.execute("UPDATE pm_episode_heads SET head_id=? WHERE project=? AND logical_episode_id=? AND head_id=?", (obj["id"], durable["project"], logical, target_id)).rowcount != 1:
                            raise ValueError("consolidation head changed")
                        relation = {"type": "summarizes", "from": obj["id"], "to": target_id, "evidence": obj["evidence"]["consolidation_of"]}
                        relation["id"] = "pmr_" + _hash([job["id"], relation])[:40]
                        relations.append(relation)
                for rel in relations:
                    c.execute("INSERT INTO pm_relations VALUES (?,?,?,?,?,?)", (rel["id"], durable["project"], rel["from"], rel["to"], rel["type"], _json(rel)))
                    c.executemany("INSERT INTO pm_relation_evidence VALUES (?,?)", [(rel["id"], sid) for sid in {r["source_id"] for r in rel["evidence"]}])
                for source in durable["sources"] if durable.get("job_kind") != "reconsideration" else []:
                    if source.get("historical"):
                        continue
                    from .project_memory_custody import record_consumed
                    record_consumed(c, durable["project"], source)
                    offset = source["body_offset"]+len(source["body"])
                    c.execute("UPDATE pm_events SET offset=?,done=? WHERE project=? AND source_id=?", (offset, int(offset>=source["body_length"]), durable["project"], source["id"]))
                from .project_memory_custody import associate_claims
                associate_claims(c, durable["project"], objects)
                revision = durable["revision"]+1
                c.execute("UPDATE pm_projects SET revision=? WHERE project=?", (revision, durable["project"]))
                c.execute("INSERT INTO pm_revisions VALUES (?,?,?,?,?)", (durable["project"], revision, job["id"], max(s["seq"] for s in durable["sources"]), time.time()))
                if durable.get("job_kind") == "reconsideration" and not any(obj.get("consolidated_episode_ids") for obj in objects):
                    reconciliation["pending_reconciliation"] = True
                result = {"status": "committed", "job_kind": durable.get("job_kind", "events"), "job_id": job["id"], "revision": revision, "object_ids": [o["id"] for o in objects], "relation_ids": [r["id"] for r in relations], "processing": _safe_receipt(receipt), **reconciliation}
                c.execute("UPDATE pm_jobs SET status='completed',receipt_json=?,input_json='{}' WHERE id=?", (_json(result), job["id"]))
                return result
            return self.store._write(commit_job)

    def get(self, project: Any, ids: list[str]) -> list[dict[str, Any]]:
        project = self._project(project)
        with self.store._lock:
            def read():
                c = self.store._connection
                result = []
                for object_id in dict.fromkeys(ids):
                    row = c.execute("SELECT data_json FROM pm_objects WHERE project=? AND id=?", (project, object_id)).fetchone()
                    if row:
                        item = json.loads(row[0])
                        _capture_order(c, project, item)
                        if item["object_type"] == "episode":
                            _episode_identity(c, project, item)
                        item["base_workflow_state"] = item.get("workflow_state")
                        _apply_transitions(c, project, item)
                        result.append(item)
                    else:
                        row = c.execute("SELECT data_json FROM pm_relations WHERE project=? AND id=?", (project, object_id)).fetchone()
                        if row:
                            item = json.loads(row[0])
                            item["evidence"], item["evidence_omitted"] = _bounded_evidence(item["evidence"])
                            result.append(item)
                return result
            return self.store._read(read)

    def forget(self, project: Any, ids: list[str]) -> dict[str, Any]:
        with self.store._lock:
            return self.store._write(lambda: forget_objects(self.store._connection, self._project(project), ids))


def forget_objects(connection: sqlite3.Connection, project: str, ids: list[str]) -> dict[str, Any]:
    """Delete exact derived IDs and dependants inside the caller's transaction."""
    from .store import project_key
    project = project_key(project)
    objects, relations = set(), set()
    for object_id in set(ids):
        if connection.execute("SELECT 1 FROM pm_objects WHERE project=? AND id=?", (project, object_id)).fetchone():
            objects.add(object_id)
        if connection.execute("SELECT 1 FROM pm_relations WHERE project=? AND id=?", (project, object_id)).fetchone():
            relations.add(object_id)
    if not objects and not relations:
        return {"deleted": 0, "ids": []}
    requested = objects | relations
    count = _delete_objects(connection, project, objects)
    for relation_id in relations:
        connection.execute("DELETE FROM pm_relations WHERE project=? AND id=?", (project, relation_id))
    connection.execute("UPDATE pm_projects SET revision=revision+1,deletion_generation=deletion_generation+1 WHERE project=?", (project,))
    connection.execute("DELETE FROM pm_jobs WHERE project=?", (project,))
    connection.execute("UPDATE pm_events SET blocked_job_id=NULL WHERE project=?", (project,))
    return {"deleted": count+len(relations), "ids": sorted(requested)}


def _normalized_phrase(value: str) -> str:
    return " ".join(re.findall(r"[\w-]+", value.casefold()))


def _grounded_field(refs: list[dict], sources: dict[str, dict]) -> tuple[str, list[dict], dict]:
    """Assemble a field from its own disclosed source sentences, never paraphrase."""
    labels = {"user_intent": "User intent", "assistant_report": "Assistant report",
              "tool_record": "Tool record", "lifecycle_marker": "Lifecycle marker",
              "derived_note": "Derived note", "imported_memory": "Imported memory",
              "unclassified": "Unclassified source"}
    lines, evidence, seen = [], [], set()
    partial = False
    for ref in refs:
        source = sources.get(ref["source_id"])
        if source is None:
            raise ValueError("grounded fields require disclosed original sources")
        body, offset = source["body"], source["body_offset"]
        start, end = ref["start"] - offset, ref["end"] - offset
        if not 0 <= start < end <= len(body) or body[start:end] != ref["quote"]:
            raise ValueError("grounded fields require exact disclosed source spans")
        sentences = [sentence for sentence in re.finditer(r".+?(?:[.!?](?=\s|$)|\n|$)", body, re.S)
                     if sentence.end() > start and sentence.start() < end and sentence.group().strip()]
        if not sentences:
            continue
        left, right = sentences[0].start(), sentences[-1].end()
        key = (ref["source_id"], offset + left, offset + right)
        if key in seen:
            continue
        seen.add(key)
        incomplete = (left == 0 and offset > 0 or
                      right == len(body) and offset + len(body) < source.get("body_length", offset + len(body)))
        partial = partial or incomplete
        role = source["source_role"]
        label = labels.get(role, "Unclassified source")
        if incomplete:
            label += " (partial context)"
        lines.append(label + ": " + body[left:right].strip())
        evidence.append({**ref, "start": key[1], "end": key[2], "quote": body[left:right],
                         "source_role": role})
    text = "\n".join(lines)
    if not text or len(text) > 6000 or sum(len(ref["quote"]) for ref in evidence) > 6000 or len(evidence) > 32:
        raise ValueError("grounded field exceeds complete excerpt bounds")
    return text, evidence, {"basis": "attributed_source_excerpts",
                            "field_association": "model_inference",
                            "source_roles": list(dict.fromkeys(ref["source_role"] for ref in evidence)),
                            "partial_context": partial}


def _explicit_transition(evidence: list[dict], relation: str, contexts: dict[str, str] | None = None, *, subject: str | None = None) -> bool:
    """Conservative lexical floor, not a semantic truth or verification claim."""
    pattern = (r"\b(completed|closed|resolved|finished|deployed|passed|done)\b|заверш[её]н|закрыт|выполнен|прошл[аио]" if relation == "closes" else r"\b(replace[sd]?|supersede[sd]?|instead|no longer)\b|замен|вместо|отмен")
    normalized_subject = _normalized_phrase(subject) if subject is not None else ""
    for ref in evidence:
        if ref["source_role"] not in {"user_intent", "tool_record"} or not re.search(pattern, ref["quote"], re.I):
            continue
        if normalized_subject and normalized_subject not in _normalized_phrase(ref["quote"]):
            continue
        for sentence in _evidence_sentences(ref, (contexts or {}).get(ref["source_id"], ref["quote"])):
            # Subject, positive status, and absence of negation must come from
            # the same source sentence and the same evidence reference.
            if normalized_subject and normalized_subject not in _normalized_phrase(sentence):
                continue
            if re.search(pattern, sentence, re.I) and not _nonassertive_context(sentence):
                return True
    return False


def _safe_receipt(receipt: dict[str, Any] | None) -> dict[str, Any]:
    """Persist accounting/profile fields, never arbitrary response/error text."""
    allowed = {"code", "reason_code", "job_kind", "historical_sources_omitted", "reconsideration_sources_omitted", "processor_id", "output_schema_version", "model", "reasoning_effort", "worker_thread_id", "worker_turn_id", "duration_ms", "input_tokens", "output_tokens", "cached_input_tokens", "cost_usd", "model_calls", "omitted_fields_count", "omitted_fields_reason", "omitted_fields"}
    return {k: v for k, v in (receipt or {}).items() if k in allowed and isinstance(v, (str, int, float, bool)) and (not isinstance(v, str) or len(v) <= 200)}


def _salient_user_signal(body: str) -> int:
    """Priority from explicit user wording; never acceptance or permission."""
    kind = _user_signal_kind(body)
    if kind == "correction":
        return 5
    if kind == "remember_request":
        return 4
    if kind == "decision_intent":
        return 3
    if re.search(r"\b(important|важно|обязательно)\b", body, re.I):
        return 2
    if re.search(r"\b(must|do not|don't|не)\b", body, re.I):
        return 1
    return 0


def _user_signal_kind(body: str) -> str:
    if re.search(r"\b(correction|correct the previous|исправление|поправка|исправь)\b", body, re.I):
        return "correction"
    if re.search(r"(?:^|\n|[.!?]\s+)(?:please\s+)?remember\b|\bkeep (?:this |that )?in memory\b|\b(?:don't|do not) forget\b|\bзапомни(?:ть)?\b|\bсохрани (?:это )?в памяти\b|\bне забудь\b", body, re.I):
        return "remember_request"
    if re.search(r"\bdecision (?:is |was )?(?:accepted|approved)\b|\bwe (?:have )?(?:agreed|decided|approved)\b|\bI (?:accept|approve)\b|\baccepted decision\b|\bрешение (?:принято|согласовано)\b|\bмы (?:решили|согласовали)\b|\bя (?:принимаю|согласен)\b", body, re.I):
        return "decision_intent"
    return "user_message"


def _readiness_sql() -> str:
    """The same durable lane condition is shared by discovery and leasing."""
    return (" AND EXISTS (SELECT 1 FROM pm_events rv JOIN entries re ON re.id=rv.source_id WHERE rv.project=v.project AND rv.session_id=v.session_id AND rv.done=0 AND rv.blocked_job_id IS NULL GROUP BY rv.project,rv.session_id HAVING "
            "MAX(CAST(strftime('%s',re.created_at) AS INTEGER))<=? OR MIN(CAST(strftime('%s',re.created_at) AS INTEGER))<=? OR COUNT(*)>=32 OR SUM(MAX(0,length(re.body)-rv.offset))>=24000 OR "
            "(MAX(CASE WHEN codex_mem_source_role(re.source)='assistant_report' OR codex_mem_source_role(re.source)='lifecycle_marker' THEN 1 ELSE 0 END)=1 AND MAX(CAST(strftime('%s',re.created_at) AS INTEGER))<=?))")


def _nonassertive_context(text: str, decision: bool = False) -> bool:
    if not text.strip() or "?" in text:
        return True
    uncertain = r"\b(not|no|never|failed|pending|unverified|incomplete|without|will|would|should|could|might|if|whether|when|plan|planned|expect|maybe|perhaps|claimed|reported|says|said|prints?|printed|echo|false|fictional|hypothetical|example)\b|\bне\b|непровер|неудач|\bбудет\b|\bесли\b|планиру|возможно|предполож|напечат|утвержда"
    if re.search(uncertain, text, re.I):
        return True
    return bool(decision and re.search(r"\b(propose|proposed|proposal|suggest|suggestion|consider|considering|option)\b|предлага|предлож|рассмотр", text, re.I))


def _fts_query(query: str, project: str | None = None, *, topic_query: bool = False) -> str:
    stop = {"the", "a", "an", "to", "of", "in", "on", "is", "are", "what", "why", "how", "tell", "me", "about", "status", "state", "and", "or", "for", "with", "project", "current", "что", "как", "где", "статус", "проект", "проекта", "по", "на", "и"}
    tokens = [token for token in re.findall(r"[\w.-]+", str(query).casefold()) if len(token)>1 and token not in stop][:16]
    from .jev_retrieval import exact_constraints
    constraints = exact_constraints(query, project)
    exact = list(dict.fromkeys([*constraints["identifiers"], *constraints["versions"]]))
    ordinary = [token for token in tokens if not any(token in identifier.casefold() for identifier in exact)]
    quote = lambda token: '"'+token.replace('"', '""')+'"'
    groups = [quote(token) for token in exact]
    if ordinary:
        if topic_query:
            # The caller has already removed question/status framing. Every
            # remaining topic must match before the bounded candidate limit.
            # Prefixes apply only to ordinary alphabetic topic stems, never exact anchors.
            groups.extend(quote(token)+("*" if re.fullmatch(r"(?:[a-z]{4,}|[а-яё]{3,})", token) else "") for token in ordinary)
        else:
            groups.append("("+" OR ".join(quote(token) for token in ordinary)+")")
    return " AND ".join(groups)


def _bounded_evidence(refs: list[dict], budget: int = 2048) -> tuple[list[dict], int]:
    selected = []
    for ref in refs:
        if len(selected) >= 8 or budget <= 0:
            break
        item = dict(ref)
        quote = item.get("quote", "")
        if len(quote) > budget:
            item["quote"] = quote[:budget]
            item["end"] = item["start"] + budget
            item["truncated"] = True
        budget -= len(item.get("quote", ""))
        selected.append(item)
    return selected, max(0, len(refs)-len(selected))


def _apply_transitions(connection: sqlite3.Connection, project: str, item: dict, exclude_session_id: str | None = None) -> None:
    predicate = "r.project=? AND r.to_id=? AND r.type IN ('closes','replaces') AND (? IS NULL OR NOT EXISTS (SELECT 1 FROM pm_relation_evidence p JOIN entries e ON e.id=p.source_id WHERE p.relation_id=r.id AND e.session_id=?))"
    parameters = (project, item["id"], exclude_session_id, exclude_session_id)
    count = connection.execute("SELECT COUNT(*) FROM pm_relations r WHERE "+predicate, parameters).fetchone()[0]
    if not count:
        return
    rows = connection.execute("SELECT r.data_json FROM pm_relations r WHERE "+predicate+" ORDER BY CASE r.type WHEN 'replaces' THEN 1 ELSE 0 END DESC,r.rowid DESC LIMIT 8", parameters).fetchall()
    first = json.loads(rows[0][0])
    item["workflow_state"] = "superseded" if first["type"] == "replaces" else "completed"
    refs = [ref for row in rows for ref in json.loads(row[0])["evidence"]]
    item["transition_evidence"], item["transition_evidence_omitted"] = _bounded_evidence(refs)
    item["transition_count"] = count
    item["transitions_omitted"] = max(0, count-len(rows))


def _evidence_sentences(ref: dict, source: str) -> list[str]:
    """Expand a cited span to complete source sentences, including questions."""
    start, end = ref.get("start", 0), ref.get("end", len(source))
    sentences = []
    previous = ""
    for match in re.finditer(r".+?(?:[.!?](?=\s|$)|\n|$)", source, re.S):
        text = match.group(0).strip()
        if match.start() < end and match.end() > start:
            sentences.append((previous+" "+text) if previous.endswith(":") else text)
        previous = text
    return sentences


def _accepted_evidence(ref: dict, source: str) -> bool:
    sentences = _evidence_sentences(ref, source)
    explicit = r"\b(accepted|approved|agreed|decided|chosen)\b|принят|согласован|решили|выбрали"
    if any(re.search(explicit, sentence, re.I) and not _nonassertive_context(sentence, decision=True) for sentence in sentences):
        return True
    return len(sentences) == 1 and not _nonassertive_context(sentences[0], decision=True)


_ATTEMPT_COUNTERS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens", "total_tokens")


def _attempt_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _attempt_identifier(value: str, field: str, *, empty: bool = False) -> str:
    if empty and value == "":
        return value
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value):
        raise ValueError(f"invalid attempt {field}")
    return value


def _attempt_metrics(receipt: dict[str, Any]) -> dict[str, Any]:
    """Normalize a native checkpoint or flattened receipt without copying text."""
    if not isinstance(receipt, dict):
        receipt = {}
    metrics = receipt.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else receipt
    usage = metrics.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    nested_tokens = usage.get("tokens", metrics.get("tokens"))
    nested_tokens = nested_tokens if isinstance(nested_tokens, dict) else {}
    aliases = {"cache_write_tokens": "cache_write_input_tokens", "reasoning_tokens": "reasoning_output_tokens"}
    normalized = {}
    for field in _ATTEMPT_COUNTERS:
        alias = aliases.get(field, field)
        value = nested_tokens.get(field, nested_tokens.get(alias, metrics.get(field, metrics.get(alias, receipt.get(field, receipt.get(alias))))))
        if type(value) is int and 0 <= value <= 2**63-1:
            normalized[field] = value
    known = bool(normalized)
    status = receipt.get("usage_status", usage.get("status", metrics.get("usage_status")))
    normalized["usage_status"] = ("reported" if status == "reported" and normalized.get("input_tokens") is not None and normalized.get("output_tokens") is not None else "partial" if known else "unknown")
    source = receipt.get("usage_source", usage.get("source", metrics.get("usage_source")))
    normalized["usage_source"] = source if isinstance(source, str) and source in {"app_server_thread_total", "native_worker", "runner_receipt", "native_checkpoint", "injected_runner"} else "runner_receipt" if known else "unknown"
    updates = receipt.get("usage_updates", usage.get("updates", 0))
    normalized["usage_updates"] = updates if type(updates) is int and 0 <= updates <= 2**31-1 else 0
    duration = metrics.get("duration_ms", receipt.get("runner_duration_ms", receipt.get("duration_ms")))
    if type(duration) in (int, float) and 0 <= duration <= 2**63-1:
        normalized["duration_ms"] = round(duration)
    for field in ("worker_thread_id", "worker_turn_id"):
        value = receipt.get(field)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", value):
            normalized[field] = value
    error = receipt.get("error_code", receipt.get("code"))
    if error is not None:
        normalized["error_code"] = error if isinstance(error, str) and error in {"configuration_error", "invalid_response", "timeout", "runner_failure", "unavailable", "storage_failure", "source_changed", "configuration_changed", "jev_quality_uncertain", "unsupported_model", "auth_error", "cli_not_found", "interrupted", "cancelled", "unknown"} else "processing_failed"
    return normalized


def _capture_order(connection: sqlite3.Connection, project: str, item: dict[str, Any]) -> None:
    row = connection.execute("SELECT MIN(v.seq),MAX(v.seq) FROM pm_evidence e JOIN pm_events v ON v.source_id=e.source_id WHERE e.object_id=? AND v.project=?", (item["id"], project)).fetchone()
    item.update(source_capture_seq_min=row[0], source_capture_seq_max=row[1], capture_order_basis="local_capture_sequence_not_event_time")


_EXECUTION_TOOLS = frozenset({"bash", "shell", "shell_command", "exec_command", "functions.shell", "functions.shell_command", "functions.exec_command"})


def _execution_result_signal(connection: sqlite3.Connection, project: str, source: sqlite3.Row) -> dict[str, Any] | None:
    from .tool_io import get_tool_capture_for_entry, is_private_prompt
    capture = get_tool_capture_for_entry(connection, source["id"], project)
    if not capture or capture["session_id"] != source["session_id"] or str(capture["tool_name"]).lower() not in _EXECUTION_TOOLS:
        return None
    if any(capture.get(name, {}).get("truncated") or capture.get(name, {}).get("excluded") or capture.get(name, {}).get("memory_context_omitted") for name in ("input_metadata", "response_metadata")):
        return None
    try:
        inputs, response = json.loads(capture["tool_input"]), json.loads(capture["tool_response"])
    except (TypeError, ValueError):
        return None
    if not isinstance(inputs, dict) or not isinstance(response, dict) or is_private_prompt(_json([inputs, response])):
        return None
    command_field = next((field for field in ("command", "cmd") if isinstance(inputs.get(field), str) and inputs[field].strip()), None)
    exit_field = next((field for field in ("exit_code", "exitCode", "returncode", "return_code") if type(response.get(field)) is int and -(2**31)<=response[field]<2**31), None)
    result_field = next((field for field in ("output", "stdout", "stderr") if isinstance(response.get(field), str)), None)
    if command_field is None or exit_field is None or result_field is None:
        return None
    command, result = inputs[command_field], response[result_field]
    if "[REDACTED]" in command:
        return None
    command_excerpt, result_excerpt = command[:300], result[:600]
    def evidence(field, quote):
        return {"source_id": source["id"], "tool_use_id": capture["tool_use_id"], "source_field": field, "start": 0, "end": len(quote), "quote": quote, "source_role": "tool_record", "session_id": source["session_id"]}
    return {"id": source["id"], "source_id": source["id"], "session_id": source["session_id"], "seq": source["seq"], "provisional": True, "source_role": "tool_record", "signal_kind": "observed_execution_result", "selection_basis": "captured_execution_shape", "text": f"Captured execution result: {command_excerpt}\nExit code: {response[exit_field]}\nResult excerpt: {result_excerpt}", "truncated": len(command)>300 or len(result)>600,
            "provenance": {"tool_use_id": capture["tool_use_id"], "tool_name": capture["tool_name"], "command": command_excerpt, "command_field": "tool_input."+command_field, "exit_code": response[exit_field], "exit_field": "tool_response."+exit_field, "result_field": "tool_response."+result_field, "result_excerpt": result_excerpt, "capture_recorded_at": capture["created_at"]},
            "evidence": {"execution_command": [evidence("tool_input."+command_field, command_excerpt)], "execution_result": [evidence("tool_response."+result_field, result_excerpt)]}}


def _episode_identity(connection: sqlite3.Connection, project: str, item: dict[str, Any]) -> None:
    logical = item.get("logical_episode_id") or item["id"]
    head = connection.execute("SELECT head_id FROM pm_episode_heads WHERE project=? AND logical_episode_id=?", (project, logical)).fetchone()
    item.update(logical_episode_id=logical, episode_version=item.get("episode_version", 1), parent_episode_id=item.get("parent_episode_id"), continuity_basis=item.get("continuity_basis", "standalone"), is_current_head=(head[0] == item["id"]) if head else logical == item["id"])


_REOPENING_MENTION = r"\breopen(?:ed|ing)?\b|\bпереоткр[а-яё]*\b|\bповторно\s+откр[а-яё]*\b"


def _task_evidence_sentences(ref: dict, source: str) -> list[str]:
    """Keep reopening qualifiers across soft wraps, with original offsets."""
    # UserPromptSubmit capture adds this exact leading structural header.
    # Blank it in the recognition copy only, keeping every source offset.
    header = "[User prompt]\n"
    if source.startswith(header):
        source = " " * (len(header) - 1) + "\n" + source[len(header):]
    if "\n" not in source:
        return _evidence_sentences(ref, source)

    def join_reopening_wraps(match):
        text = match.group(0)
        # Replacing one character preserves citation offsets. Other source
        # statements keep their existing line and punctuation boundaries.
        return text.replace("\n", " ") if re.search(_REOPENING_MENTION, text, re.I) else text

    unwrapped = re.sub(r".+?(?:[.!?](?=\s|$)|$)", join_reopening_wraps, source, flags=re.S)
    return _evidence_sentences(ref, unwrapped)


def _source_task_identity(item: dict, contexts: dict[str, str]) -> str | None:
    """Require one affirmative user task identity in the original evidence."""
    identities = set()
    for field in ("goal", "summary"):
        for ref in item.get("evidence", {}).get(field, []):
            if ref.get("source_role") != "user_intent":
                continue
            for sentence in _task_evidence_sentences(ref, contexts.get(ref["source_id"], "")):
                if "?" in sentence or re.search(r"\b(?:not|never|unrelated|different|separate|example|fixture)\b|не\s|друг(?:ая|ой|ую)|отдельн|пример", sentence, re.I):
                    continue
                found = {value.casefold() for value in re.findall(TASK_ID_PATTERN, sentence)}
                if re.search(_REOPENING_MENTION, sentence, re.I) and (
                        len(found) != 1 or re.search(r"```|~~~", contexts.get(ref["source_id"], ""))
                        or not _positive_reopening(sentence, next(iter(found)))):
                    continue
                if len(found) == 1 and (re.match(r"\s*" + TASK_ID_PATTERN, sentence) or re.search(r"(?:^|\b)(?:task|issue|ticket|continue|resume|correction|update|задача|задачи|задачу|продолжаем|исправление|уточнение)\b", sentence, re.I)
                                       or _positive_reopening(sentence, next(iter(found)))):
                    identities.update(found)
    return next(iter(identities)) if len(identities) == 1 else None


def _positive_reopening(sentence: str, task_id: str) -> bool:
    """Recognize complete affirmative commands, not mentions or conditions."""
    identity = re.escape(task_id)
    modifier = r"(?!(?:only|if|after|before|when|unless|until|without|is|are|was|were|must|should|would|could|can|will|not|no|never)\b)[A-Za-z][A-Za-z0-9-]*"
    description = r"same(?:\s+" + modifier + r"){0,4}\s+(?:task|ticket|issue|work)"
    # The ID supplies identity; the comma appositive only describes it. Keep
    # it short and exclude qualifiers, clauses and references to commands.
    appositive_word = (r"(?!(?:only|if|after|before|when|unless|until|without|is|are|was|were|must|should|would|could|might|may|can|will|not|no|never|"
                       r"once|provided|assuming|pending|awaiting|and|or|but|quote|example|reference|label|instruction|command|help|"
                       r"maybe|possibly|perhaps|that|which|whose|where)\b)[A-Za-z]+(?:-[A-Za-z]+)*")
    appositive = r",\s*(?:the\s+)?same(?:\s+" + appositive_word + r"){1,4}"
    english = (r"(?:please,?\s+)?reopen\s+(?:the\s+)?(?:(?:" + description +
               r"|task|ticket|issue)\s+)?" + identity +
               r"(?:\s*(?:(?:,\s*|\s+as\s+)(?:the\s+)?" + description + r"|" + appositive + r"))?")
    russian = (r"(?:пожалуйста,?\s+)?(?:переоткрой(?:те)?|повторно\s+открой(?:те)?)\s+"
               r"(?:(?:ту\s+же\s+|эту\s+же\s+)?задачу\s+)?" + identity +
               r"(?:\s*,\s*(?:ту|эту)\s+же\s+задачу)?")
    # One short explanation can describe the purpose of an unconditional
    # command. Qualifiers and reported/quoted commands remain unsupported.
    clause_word = (r"(?!(?:only|if|after|before|when|whenever|unless|until|without|not|no|never|"
                   r"once|provided|assuming|pending|awaiting|and|or|but|quote|example|reference|instruction|command|help|"
                   r"maybe|possibly|perhaps|might|may|must|should|would|could|can|will|"
                   r"new|different|separate|unrelated|independent|forbidden|prohibited|"
                   r"approv[a-z]*|authori[sz][a-z]*|permit[a-z]*|permission|consent|confirmation|conditional|contingent|subject)\b)"
                   r"[A-Za-z]+(?:-[A-Za-z]+)*")
    explanation = r"(?:for|to|because)\s+" + clause_word + r"(?:\s+" + clause_word + r"){0,11}"
    english_with_clause = r"(?:" + english + r"(?:\s+" + explanation + r")?|" + explanation + r",\s*" + english + r")"
    russian_word = (r"(?!(?:не|нет|нельзя|запрещено|только|если|после|перед|когда|пока|без|при|"
                    r"соглас[а-яё]*|одобр[а-яё]*|разреш[а-яё]*|авторизац[а-яё]*|"
                    r"ожида[а-яё]*|возможно|вероятно|может|нужно|надо|"
                    r"пример[а-яё]*|команд[а-яё]*|инструкц[а-яё]*|справк[а-яё]*|ссылк[а-яё]*|"
                    r"нов[а-яё]*|друг[а-яё]*|отдельн[а-яё]*|независим[а-яё]*|и|или|но)\b)"
                    r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*")
    russian_explanation = r"(?:для|чтобы|потому\s+что)\s+" + russian_word + r"(?:\s+" + russian_word + r"){0,11}"
    russian_with_clause = (r"(?:" + russian + r"(?:(?:\s+|,\s*)" + russian_explanation + r")?|"
                           + russian_explanation + r",\s*" + russian + r")")
    return bool(re.fullmatch(r"\s*(?:" + english_with_clause + "|" + russian_with_clause + r")\s*[.!]?\s*", sentence, re.I))


def _continuity_supported(refs: list[dict], prior: dict, contexts: dict[str, str]) -> bool:
    # This is a deliberately conservative identity proposal, not proof of work
    # completion. Fuzzy topic linking and same-session linking are deferred.
    task_pattern = r"(?<![\w-])[A-Za-z][A-Za-z0-9]{1,15}-[0-9]{1,10}(?![\w-])"
    reopen_marker = r"\breopen(?:ed|ing)?\b|\bпереоткрой(?:те)?\b|\bповторно\s+открой(?:те)?\b"
    update_marker = r"^\s*(?:correction|update|amendment)\s+(?:(?:for|to|on)\s+)?(?:task\s+)?|^\s*(?:исправление|поправка|уточнение|обновление)\s+(?:(?:для|по)\s+)?(?:задач[еиу]\s+)?"
    # A named goal is authoritative. Incidental identifiers in a summary or
    # its evidence must never expand the identity of that goal.
    task_ids = {value.casefold() for value in re.findall(task_pattern, prior.get("goal") or "")}
    if not task_ids:
        # With no named goal, require one unambiguous positive task identity.
        # Inspect full retained source sentences so a sliced quote cannot hide
        # "unrelated to" or another negative qualifier.
        candidates = []
        for field in ("goal", "summary"):
            for evidence in prior.get("evidence", {}).get(field, []):
                context = contexts.get(evidence["source_id"])
                if context is not None:
                    candidates.extend(_task_evidence_sentences(evidence, context))
        for text in candidates:
            for sentence in re.findall(r".+?(?:[.!?](?=\s|$)|\n|$)", text, re.S):
                if "?" in sentence or re.search(r"\b(?:not|never|unrelated|different|separate|instead)\b|не\s|друг(?:ая|ой|ую)|отдельн|не\s+связан", sentence, re.I):
                    continue
                identities = re.findall(task_pattern, sentence)
                if len(set(value.casefold() for value in identities)) != 1:
                    # Multiple positive named tasks are ambiguous even when
                    # they share one category or appear in one sentence.
                    task_ids.update(value.casefold() for value in identities)
                    continue
                identity = re.escape(identities[0])
                if re.search(r"^\s*"+identity+r"\b|\b(?:task|issue|ticket|continue|resume|continuing|resuming|задача|задачи|задачу|продолжаем|продолжить)\s+"+identity+r"\b|(?:"+update_marker+r")"+identity+r"\b", sentence, re.I):
                    task_ids.add(identities[0].casefold())
    if len(task_ids) > 1:
        return False
    goal_text = prior.get("goal", "")
    if prior.get("field_provenance", {}).get("goal", {}).get("basis") == "attributed_source_excerpts":
        goal_text = " ".join(ref["quote"] for ref in prior.get("evidence", {}).get("goal", []))
    goal = _normalized_phrase(goal_text)
    marker = r"\b(continue|continuing|continuation|resume|resuming|follow-up|follow up|same task)\b|продолжа|возвращаемся|та же задача|той же задач"
    named_same_task = None
    if len(task_ids) == 1:
        named_same_task = r"same\s+" + re.escape(next(iter(task_ids))) + r"\s+(?:task|ticket|issue)\b"
        marker += r"|\b" + named_same_task
    for ref in refs:
        if ref["source_role"] not in {"user_intent", "assistant_report", "tool_record"}:
            continue
        quoted_continuation = bool(re.search(marker, ref["quote"], re.I))
        quoted_update = ref["source_role"] == "user_intent" and bool(re.search(update_marker, ref["quote"], re.I))
        quoted_reopen = (ref["source_role"] == "user_intent" and len(task_ids) == 1
                         and bool(re.search(reopen_marker, ref["quote"], re.I)))
        if not quoted_continuation and not quoted_update and not quoted_reopen:
            continue
        for sentence in _task_evidence_sentences(ref, contexts.get(ref["source_id"], "")):
            sentence_reopening = bool(re.search(_REOPENING_MENTION, sentence, re.I))
            if sentence_reopening:
                # Reopening is user authority, not a reported command or a
                # quoted example. Apply this before every continuity branch.
                if (ref["source_role"] != "user_intent" or len(task_ids) != 1
                        or re.search(r"```|~~~", contexts.get(ref["source_id"], ""))
                        or not _positive_reopening(sentence, next(iter(task_ids)))):
                    continue
            denied = r"\b(?:not|never)\s+(?:(?:a|an|the)\s+)?(?:continu\w*|resum\w*|same\s+task|follow[- ]up|correction|update|amendment)\b|\b(?:new|different|separate|unrelated|independent)\s+(?:task|ticket|issue)\b|не\s+(?:является\s+)?(?:продолж\w*|той\s+же\s+задач\w*|та\s+же\s+задач\w*)|(?:новая|другая|отдельная)\s+задача"
            if named_same_task:
                denied += r"|\b(?:not|never)\s+(?:(?:a|an|the)\s+)?" + named_same_task
            if "?" in sentence or re.search(denied, sentence, re.I):
                continue
            # A matching label or reference identifies mentioned text, not
            # the task being continued. Leave that association unresolved.
            if (not (quoted_reopen and sentence_reopening)
                    and named_same_task and re.search(named_same_task, sentence, re.I)
                    and re.search(r"\b(?:labels?|references?|identifiers?)\b|\brefer(?:s|red|ring)?\s+to\b|\b(?:ссылк|метк|идентификатор)[а-яё]*\b", sentence, re.I)):
                continue
            continuation = quoted_continuation and bool(re.search(marker, sentence, re.I))
            update = quoted_update and bool(re.search(update_marker, sentence, re.I))
            # The complete-command gate above owns reopening authority. A
            # failed check in its reason does not negate the user command.
            reopen = quoted_reopen and bool(re.search(reopen_marker, sentence, re.I))
            if not continuation and not update and not reopen:
                continue
            quoted_ids = {value.casefold() for value in re.findall(task_pattern, ref["quote"])}
            sentence_ids = {value.casefold() for value in re.findall(task_pattern, sentence)}
            if len(sentence_ids) > 1:
                continue
            if sentence_ids and task_ids and sentence_ids != task_ids:
                continue
            if len(task_ids) == 1 and task_ids == sentence_ids and task_ids & quoted_ids:
                return True
            if continuation and len(goal.split()) >= 2 and goal in _normalized_phrase(ref["quote"]) and goal in _normalized_phrase(sentence):
                return True
    return False


def _bounded_generation_state(state: dict[str, Any], sources: list[dict[str, Any]], budget: int = 24000) -> dict[str, Any]:
    """Reserve current heads and open obligations without trimming evidence."""
    groups = ("claims", "episodes", "relations")
    candidates = {group: list(state[group]) for group in groups}
    obligations = list(state.get("project_obligations", []))
    existing = {item["id"] for item in candidates["claims"]}
    candidates["claims"].extend(item for item in obligations if item["id"] not in existing)
    obligation_ids = {item["id"] for item in obligations}
    terms = set(re.findall(r"[\w-]{4,}", " ".join(source["body"] for source in sources).casefold()))
    task_pattern = r"(?<![\w-])[A-Za-z][A-Za-z0-9]{1,15}-[0-9]{1,10}(?![\w-])"
    task_ids = set(re.findall(task_pattern, " ".join(source["body"] for source in sources).casefold()))
    def relevance(item):
        text = " ".join(str(item.get(field) or "") for field in ("goal", "summary", "text"))
        named = set(re.findall(task_pattern, text.casefold()))
        words = set(re.findall(r"[\w-]{4,}", text.casefold()))
        return (len(task_ids & named), len(terms & words), item.get("revision", 0))
    heads = sorted(candidates["episodes"], key=relevance, reverse=True)
    other_claims = sorted((item for item in candidates["claims"] if item["id"] not in obligation_ids), key=lambda item: (item.get("workflow_state") in {"open", "blocked", "accepted"}, relevance(item)), reverse=True)
    selected = {**state, **{group: [] for group in groups}, "project_obligations": []}
    metadata = {"method": "reserved_heads_and_open_obligations", "budget_ascii_chars": budget,
                "serialized_ascii_chars": 0, "candidate_window_bounded": True,
                "considered_records": {group: len(candidates[group]) for group in groups},
                "omitted_records": {group: len(candidates[group]) for group in groups}}
    selected["context_selection"] = metadata
    seen = set()
    def measure():
        metadata["omitted_records"] = {group: len(candidates[group])-len(selected[group]) for group in groups}
        # The length field itself contributes a few digits to the total.
        for _ in range(3):
            metadata["serialized_ascii_chars"] = len(json.dumps(selected, ensure_ascii=True, separators=(",", ":")))
        return metadata["serialized_ascii_chars"]
    def take(group, item):
        if item["id"] in seen:
            return False
        selected[group].append(item)
        if measure() > budget:
            selected[group].pop()
            measure()
            return False
        seen.add(item["id"])
        return True
    # First retain one relevant head if it can coexist with one obligation.
    # Subsequent passes reserve half the budget for heads and a quarter for
    # obligations, then share unused room. Records remain complete throughout.
    smallest_obligation = min((len(json.dumps(item, ensure_ascii=True, separators=(",", ":"))) for item in obligations), default=0)
    head_chars = 0
    for item in heads:
        size = len(json.dumps(item, ensure_ascii=True, separators=(",", ":")))
        if measure()+size+smallest_obligation+2 <= budget and take("episodes", item):
            head_chars += size
            break
    for group, records, allowance, used in (("episodes", heads, budget//2, head_chars), ("claims", obligations, budget//4, 0), ("claims", other_claims, budget//4, 0)):
        for item in records:
            if item["id"] in seen:
                continue
            size = len(json.dumps(item, ensure_ascii=True, separators=(",", ":")))
            if used+size <= allowance and take(group, item):
                used += size
    for group, records in (("episodes", heads), ("claims", obligations), ("claims", other_claims), ("relations", candidates["relations"])):
        for item in records:
            take(group, item)
    measure()
    return selected


def _project_obligations(connection: sqlite3.Connection, project: str, limit: int, exclude_session_id: str | None) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT o.data_json FROM pm_objects o WHERE o.project=? AND o.object_type='claim' AND json_extract(o.data_json,'$.kind') IN ('requirement','constraint') AND json_extract(o.data_json,'$.workflow_state') IN ('open','blocked','accepted','proposed') AND (? IS NULL OR (o.session_id!=? AND NOT EXISTS (SELECT 1 FROM pm_evidence e JOIN entries s ON s.id=e.source_id WHERE e.object_id=o.id AND s.session_id=?))) AND NOT EXISTS (SELECT 1 FROM pm_relations r WHERE r.project=o.project AND r.to_id=o.id AND r.type IN ('closes','replaces') AND (? IS NULL OR NOT EXISTS (SELECT 1 FROM pm_relation_evidence re JOIN entries s ON s.id=re.source_id WHERE re.relation_id=r.id AND s.session_id=?))) ORDER BY o.revision,o.id LIMIT ?", (project, exclude_session_id, exclude_session_id, exclude_session_id, exclude_session_id, exclude_session_id, limit))
    result = []
    for row in rows:
        item = json.loads(row[0])
        item["base_workflow_state"] = item.get("workflow_state")
        _capture_order(connection, project, item)
        result.append(item)
    return result
