"""Read-only, project-scoped retrieval health; timestamps never imply coverage."""
from __future__ import annotations

import sqlite3
from typing import Any

from .config import automatic_capture_enabled, load_config
from .store import StoreError, project_key

_SAFE_CODES = frozenset({
    'invalid_request', 'invalid_response', 'model_mismatch', 'model_unavailable',
    'protocol_error', 'rerouted', 'runner_failure', 'runner_unavailable',
    'storage_failure', 'lease_expired', 'timeout', 'tool_called', 'tools_available',
})


def _freshness_snapshot(store: Any, project: Any) -> dict[str, Any]:
    """Inspect existing storage only; do not claim jobs, start workers or repair indexes.

    Pending counts active processor-eligible captures without a successful/skipped
    receipt, including running and failed batches. Index metrics concern ready
    records under the pinned embedding profile, independently of capture backlog.
    ``current`` means no known backlog in retained local data, not universal knowledge.
    """
    workspace = project_key(project)
    result: dict[str, Any] = {
        'status': 'unknown', 'knowledge_incomplete': None,
        'latest_capture_at': None, 'last_successful_processing_at': None,
        'last_ready_at': None, 'pending_capture_count': None, 'ready_count': None,
        'blocked_reason': None, 'capture_enabled': None, 'processing_enabled': None,
        'semantic_enabled': None,
        'index': {'indexed': None, 'pending': None, 'stale': None},
    }
    unknown = False
    config = load_config(store.data_dir)
    if getattr(config, 'valid', True):
        result['capture_enabled'] = automatic_capture_enabled(workspace, config)
        result['semantic_enabled'] = config['semantic_enabled']
        result['processing_enabled'] = bool(config['processor_enabled'] and config['service_enabled'])
    else:
        unknown = True
    try:
        with store._lock:
            store._require_open()
            connection = store._connection
            row = store._read(lambda: connection.execute('''
                SELECT MAX(CASE WHEN source GLOB 'hook:*' THEN created_at END) AS capture,
                    MAX(CASE WHEN COALESCE(source, '') NOT GLOB 'hook:*'
                        AND superseded_by IS NULL THEN updated_at END) AS ready,
                    COUNT(CASE WHEN COALESCE(source, '') NOT GLOB 'hook:*'
                        AND superseded_by IS NULL THEN 1 END) AS ready_count
                FROM entries WHERE project = ?''', (workspace,)).fetchone())
            result.update(latest_capture_at=row['capture'], last_ready_at=row['ready'], ready_count=row['ready_count'])
            result['last_successful_processing_at'] = store._read(lambda: connection.execute(
                "SELECT MAX(completed_at) FROM observation_jobs WHERE project = ? AND status IN ('processed', 'skipped')",
                (workspace,)).fetchone()[0])
            row = store._read(lambda: connection.execute('''
                SELECT COUNT(*) FROM entries e WHERE e.project = ? AND e.superseded_by IS NULL
                    AND (e.source IN ('hook:UserPromptSubmit', 'hook:Stop', 'hook:PostToolUse')
                         OR e.source LIKE 'hook:PostToolUse:%')
                    AND NOT EXISTS (SELECT 1 FROM observation_job_sources s
                        JOIN observation_jobs j ON j.id = s.job_id
                        WHERE s.source_id = e.id AND j.project = e.project
                        AND j.status IN ('processed', 'skipped'))''', (workspace,)).fetchone())
            result['pending_capture_count'] = row[0]
            failed = store._read(lambda: connection.execute('''
                SELECT j.error_code FROM observation_jobs j WHERE j.project = ? AND j.status = 'failed'
                    AND EXISTS (SELECT 1 FROM observation_job_sources s JOIN entries e ON e.id = s.source_id
                        WHERE s.job_id = j.id AND e.superseded_by IS NULL AND NOT EXISTS (
                            SELECT 1 FROM observation_job_sources s2 JOIN observation_jobs j2 ON j2.id = s2.job_id
                            WHERE s2.source_id = e.id AND j2.project = e.project AND j2.status IN ('processed', 'skipped')))
                ORDER BY j.updated_at DESC LIMIT 1''', (workspace,)).fetchone())
            if failed:
                result['blocked_reason'] = failed[0] if failed[0] in _SAFE_CODES else 'processing_failed'
    except (StoreError, sqlite3.Error, OSError):
        unknown = True
    try:
        from .service import _load_state_readonly, ServiceError
        state = _load_state_readonly(store.data_dir)
        record = state['projects'].get(workspace)
        if record and record['blocked']:
            code = record['last_code']
            result['blocked_reason'] = code if code in _SAFE_CODES else 'processing_blocked'
    except (ServiceError, OSError):
        unknown = True
    try:
        from .semantic import MODEL, MODEL_REVISION, DIMENSIONS
        index = store.embedding_status(workspace, MODEL, MODEL_REVISION, DIMENSIONS)
        result['index'] = {key: index.get(key) for key in ('indexed', 'pending', 'stale')}
        unknown |= any(value is None for value in result['index'].values())
    except (StoreError, sqlite3.Error, OSError, ValueError):
        unknown = True
    pending = result['pending_capture_count']
    index_pending = (result['index']['pending'] or 0) + (result['index']['stale'] or 0)
    if result['blocked_reason']:
        status, incomplete = 'blocked', True
    elif pending or (config.get('semantic_enabled') and index_pending):
        status, incomplete = 'lagging', True
    elif unknown:
        status, incomplete = 'unknown', None
    elif result['capture_enabled'] is False or result['processing_enabled'] is False:
        status, incomplete = 'disabled', None
    elif not result['ready_count'] and not result['latest_capture_at']:
        status, incomplete = 'empty', False
    else:
        status, incomplete = 'current', False
    result.update(status=status, knowledge_incomplete=incomplete)
    show = lambda value: 'unknown' if value is None else str(value)
    result['summary'] = (
        f"Memory {status}; knowledge incomplete={show(incomplete)}; "
        f"capture/processing/semantic enabled={show(result['capture_enabled'])}/"
        f"{show(result['processing_enabled'])}/{show(result['semantic_enabled'])}; "
        f"capture={show(result['latest_capture_at'])}; processed={show(result['last_successful_processing_at'])}; "
        f"ready={show(result['last_ready_at'])}; pending captures={show(pending)}; "
        f"index ready records indexed/pending/stale={show(result['index']['indexed'])}/"
        f"{show(result['index']['pending'])}/{show(result['index']['stale'])}; "
        f"blocker={result['blocked_reason'] or ('unknown' if unknown else 'none')}. "
        'Index coverage does not establish memory freshness.'
    )
    return result


def freshness_snapshot(store: Any, project: Any) -> dict[str, Any]:
    """Return bounded telemetry without making optional health break retrieval."""
    try:
        return _freshness_snapshot(store, project)
    except (AttributeError, TypeError, KeyError, ValueError, OSError, sqlite3.Error, StoreError):
        return {
            'status': 'unknown', 'knowledge_incomplete': None,
            'latest_capture_at': None, 'last_successful_processing_at': None,
            'last_ready_at': None, 'pending_capture_count': None, 'ready_count': None,
            'blocked_reason': None, 'capture_enabled': None, 'processing_enabled': None,
            'semantic_enabled': None,
            'index': {'indexed': None, 'pending': None, 'stale': None},
            'summary': 'Memory unknown; knowledge incomplete=unknown; capture=unknown; '
                       'processed=unknown; ready=unknown; pending captures=unknown; '
                       'capture/processing/semantic enabled=unknown/unknown/unknown; '
                       'index ready records indexed/pending/stale=unknown/unknown/unknown; '
                       'blocker=unknown. Telemetry unavailable; index coverage does not '
                       'establish memory freshness.',
        }
