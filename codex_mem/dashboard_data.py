"""Read-only, metadata-only dashboard snapshots of the existing local ledgers."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

from .config import data_dir_path
from .cost_report import build_report
from .freshness import _terminal_recovery_exclusion
from .pricing import TOKEN_FIELDS, price_event
from .store import project_key

ZONE = 'Asia/Jerusalem'
ROW_LIMIT = 500_000
QUERY_SECONDS = 8.0
SNAPSHOT_SECONDS = 20.0
CACHE_SECONDS = 30
RETRY_SECONDS = 2
TRANSIENT_FAILURES = {'query_deadline', 'database_busy'}
# Column allowlists also protect older or extended schemas from exposing content.
FIELDS = {
    'entries': 'id project kind session_id created_at updated_at superseded_by',
    'usage_sessions': 'thread_id session_id parent_thread_id project agent_path agent_role agent_nickname thread_source model_provider started_at',
    'usage_events': 'event_key thread_id session_id response_id model model_source model_provider service_tier requested_service_tier requested_service_tier_source service_tier_source recorded_at source_kind quality context_input_tokens context_cached_input_tokens context_cache_write_input_tokens context_output_tokens context_total_tokens context_window_tokens ' + ' '.join(TOKEN_FIELDS),
    'observation_jobs': 'id project processor_id model reasoning_effort session_id status disposition attempt_count worker_thread_id error_code created_at updated_at completed_at',
    'observer_usage_attempts': 'job_id attempt_count outcome error_code worker_thread_id duration_ms usage_status usage_source usage_updates started_at finished_at ' + ' '.join(TOKEN_FIELDS),
    'observation_failure_receipts': 'job_id attempt_count error_code reason_code created_at',
    'jev_filter_attempts': 'job_id attempt_count audit_json updated_at',
    'jev_judgment_audits': 'id project owner_id audit_json created_at',
}


def _instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, AttributeError, TypeError):
        return None


def _window(period):
    if period not in ('all', 'today', '7d', '30d'):
        raise ValueError('period must be all, today, 7d, or 30d')
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(ZONE))
    start = datetime(1970, 1, 1, tzinfo=timezone.utc) if period == 'all' else local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days={'today': 0, '7d': 6, '30d': 29}[period])
    return {'name': period, 'from': start.astimezone(timezone.utc).isoformat(), 'to': now.isoformat(), 'timezone': ZONE, 'basis': 'Main responses: recorded_at; observer attempts: started_at; capture: created_at; queue: current state, independent of period.', 'interval': '[from,to)'}


def _page(items, page, limit):
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError('page must be a positive integer')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    return items[(page - 1) * limit:page * limit], {'page': page, 'limit': limit, 'total': len(items), 'pages': (len(items) + limit - 1) // limit}


class DashboardReader:
    def __init__(self, data_dir=None):
        self.data_dir = data_dir_path(data_dir)
        self._cache = {}
        self._failures = {}
        self._snapshot_locks = {}
        self._lock = threading.RLock()

    def _snapshot(self, period, project=None):
        _window(period)  # Validate even on cache hits.
        cache_key = (period, project)
        with self._lock:
            lock = self._snapshot_locks.setdefault(cache_key, threading.RLock())
        # Requests for one scope share a read, without blocking other scopes.
        with lock:
            cached = self._cache.get(cache_key)
            now = time.monotonic()
            if cached and now - cached[0] < CACHE_SECONDS and cached[1]['coverage']['status'] not in TRANSIENT_FAILURES:
                return cached[1]
            failure = self._failures.get(cache_key)
            if failure and now - failure[0] < RETRY_SECONDS:
                return self._after_failure(cached, failure[1], now)
            result = self._read(period, project)
            now = time.monotonic()
            if result['status'] != 'unavailable' and result['coverage']['status'] not in TRANSIENT_FAILURES:
                self._cache[cache_key] = (now, result)
                self._failures.pop(cache_key, None)
                return result
            # A warm fallback needs only the failure reason. Retaining another
            # complete set of ledger rows increases allocation pressure on retry.
            failure = {'coverage': result['coverage']} if cached and result['coverage']['status'] in TRANSIENT_FAILURES else result
            self._failures[cache_key] = (now, failure)
            response = self._after_failure(cached, result, now)
            if not cached and result['status'] == 'partial':
                # Keep sound sections from a cold partial read as a fallback.
                # Its failure status prevents the normal healthy cache TTL.
                self._cache[cache_key] = (now, result)
            return response

    @staticmethod
    def _after_failure(cached, failed, now):
        if cached and failed['coverage']['status'] in TRANSIENT_FAILURES:
            previous = cached[1]
            return {**previous, 'status': 'stale', 'coverage': {**previous['coverage'], 'status': 'stale', 'stale': True, 'snapshot_age_seconds': max(0, now - cached[0]), 'refresh_error': failed['coverage']['status'], 'retry_after_seconds': RETRY_SECONDS}}
        return failed

    def _read(self, period, project=None):
        window = _window(period)
        result = {'period': window, 'status': 'unavailable', 'coverage': {'status': 'database_missing', 'read_only': True, 'refresh': 'not_requested', 'row_limit_per_table': ROW_LIMIT, 'snapshot_cache_seconds': CACHE_SECONDS, 'scope_project': project, 'collection_complete': False, 'basis': 'Existing local ledgers only; no source discovery or refresh.', 'truncated_tables': [], 'available_tables': [], 'stale': False, 'snapshot_at': window['to']}, 'tables': {name: [] for name in FIELDS}}
        path = self.data_dir / 'memory.sqlite3'
        if not path.is_file():
            return result
        connection = None
        try:
            connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.2)
            connection.row_factory = sqlite3.Row
            snapshot_deadline = time.monotonic() + SNAPSHOT_SECONDS
            deadline = snapshot_deadline

            def start_query():
                nonlocal deadline
                now = time.monotonic()
                if now >= snapshot_deadline or QUERY_SECONDS <= 0:
                    raise sqlite3.OperationalError('query deadline exceeded')
                deadline = min(now + QUERY_SECONDS, snapshot_deadline)

            def finish_query():
                if time.monotonic() > deadline:
                    raise sqlite3.OperationalError('query deadline exceeded')

            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            start_query()
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            available = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result['coverage']['present_tables'] = sorted(set(FIELDS) & available)
            # Main accounting excludes every known observer worker, including
            # workers whose attempt rows fall outside this project or period.
            # Read this dependency first so a later timeout cannot double count.
            result['_observer_workers'] = []
            if 'observer_usage_attempts' in available:
                observer_columns = {row[1] for row in connection.execute('PRAGMA table_info(observer_usage_attempts)')}
                if 'worker_thread_id' in observer_columns:
                    start_query()
                    workers = [row[0] for row in connection.execute('SELECT DISTINCT worker_thread_id FROM observer_usage_attempts WHERE worker_thread_id IS NOT NULL LIMIT ?', (ROW_LIMIT + 1,))]
                    finish_query()
                    result['_observer_workers'] = workers[:ROW_LIMIT]
                    result['coverage']['observer_workers_complete'] = len(workers) <= ROW_LIMIT
            for table, whitelist in FIELDS.items():
                start_query()
                if table not in available:
                    continue
                columns = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
                selected = [name for name in whitelist.split() if name in columns]
                if table == 'usage_events' and 'requested_service_tier' not in columns:
                    result['coverage']['legacy_tier_schema'] = True
                if not selected:
                    continue
                order = ' ORDER BY id DESC' if table == 'jev_judgment_audits' and 'id' in columns else ''
                expressions = [self._audit_projection() if name == 'audit_json' else name for name in selected]
                if table == 'entries' and 'source' in columns:
                    expressions.append("CASE WHEN source LIKE 'processor:%' THEN 1 ELSE 0 END AS processor_note")
                if table == 'observation_jobs':
                    entry_columns = {row[1] for row in connection.execute('PRAGMA table_info(entries)')} if 'entries' in available else set()
                    link_columns = {row[1] for row in connection.execute('PRAGMA table_info(observation_job_sources)')} if 'observation_job_sources' in available else set()
                    failure_metadata = ({'id', 'project', 'status'} <= columns
                                        and {'id', 'project', 'source', 'superseded_by'} <= entry_columns
                                        and {'job_id', 'source_id'} <= link_columns)
                    result['coverage']['active_failure_metadata_available'] = failure_metadata
                    expressions.append(self._active_failure_projection('observation_job_recoveries' in available) if failure_metadata else 'NULL AS active_failure')
                predicates, args = [], []
                if table == 'usage_events' and period != 'all' and 'recorded_at' in columns:
                    predicates.extend(('julianday(recorded_at)>=julianday(?)', 'julianday(recorded_at)<julianday(?)'))
                    args.extend((window['from'], window['to']))
                if project is not None:
                    if 'project' in columns:
                        predicates.append('project=?')
                        args.append(project)
                    elif table == 'usage_events' and 'usage_sessions' in available:
                        predicates.append('thread_id IN (SELECT thread_id FROM usage_sessions WHERE project=?)')
                        args.append(project)
                    elif table in ('observer_usage_attempts', 'jev_filter_attempts', 'observation_failure_receipts') and 'observation_jobs' in available:
                        predicates.append('job_id IN (SELECT id FROM observation_jobs WHERE project=?)')
                        args.append(project)
                predicate = ' WHERE ' + ' AND '.join(predicates) if predicates else ''
                rows = [dict(row) for row in connection.execute(f'SELECT {",".join(expressions)} FROM {table}{predicate}{order} LIMIT ?', (*args, ROW_LIMIT + 1))]
                finish_query()
                if len(rows) > ROW_LIMIT:
                    result['coverage']['truncated_tables'].append(table)
                result['tables'][table] = rows[:ROW_LIMIT]
                result['coverage']['available_tables'].append(table)
            result['pending_observations'] = None
            result['pending_oldest_at'] = None
            if {'entries', 'observation_jobs', 'observation_job_sources'} <= available:
                entry_columns = {row[1] for row in connection.execute('PRAGMA table_info(entries)')}
                if {'source', 'superseded_by'} <= entry_columns:
                    from .store import OBSERVATION_MODEL, OBSERVATION_REASONING_EFFORT
                    project_clause = ' AND e.project=?' if project else ''
                    parameters = [OBSERVATION_MODEL, OBSERVATION_REASONING_EFFORT] + ([project] if project else [])
                    start_query()
                    pending = connection.execute("SELECT e.project,COUNT(*),MIN(e.created_at) FROM entries e WHERE e.superseded_by IS NULL AND (e.source IN ('hook:UserPromptSubmit','hook:Stop','hook:PostToolUse') OR e.source LIKE 'hook:PostToolUse:%') AND NOT EXISTS (SELECT 1 FROM observation_job_sources links CROSS JOIN observation_jobs jobs ON jobs.id=links.job_id WHERE links.source_id=e.id AND jobs.project=e.project AND (jobs.status IN ('processed','skipped','running') OR (jobs.status='failed' AND NOT (COALESCE(jobs.disposition,'')='profile_retired' AND jobs.error_code='lease_expired' AND (jobs.model<>? OR jobs.reasoning_effort<>?)))))" + project_clause + ' GROUP BY e.project', parameters).fetchall()
                    finish_query()
                    result['pending_observations'] = {row[0]: row[1] for row in pending}
                    result['pending_oldest_at'] = {row[0]: row[2] for row in pending}
            result['status'] = 'partial' if result['coverage']['truncated_tables'] else 'available'
            result['coverage']['status'] = result['status']
        except (sqlite3.Error, OSError, ValueError) as error:
            result['status'] = 'unavailable'
            code = getattr(error, 'sqlite_errorcode', None)
            code = code & 0xff if code is not None else None
            # Python 3.10 has neither these named result codes nor error codes.
            # SQLite primary result codes are stable; exact messages cover older errors.
            message = str(error)
            if code == getattr(sqlite3, 'SQLITE_INTERRUPT', 9) or message in ('query deadline exceeded', 'interrupted'):
                reason = 'query_deadline'
            elif code in (getattr(sqlite3, 'SQLITE_BUSY', 5), getattr(sqlite3, 'SQLITE_LOCKED', 6)) or message in ('database is locked', 'database table is locked', 'database schema is locked'):
                reason = 'database_busy'
            elif code in (getattr(sqlite3, 'SQLITE_CORRUPT', 11), getattr(sqlite3, 'SQLITE_NOTADB', 26)) or message in ('database disk image is malformed', 'file is not a database'):
                reason = 'database_corrupt'
            else:
                reason = 'database_unreadable'
            result['coverage']['status'] = reason
            result['coverage']['retry_after_seconds'] = RETRY_SECONDS
            if reason in TRANSIENT_FAILURES and result['coverage']['available_tables']:
                # Every retained table finished in the same read transaction.
                # An optional receipt or pending-count query cannot erase them.
                result['status'] = 'partial'
                result['coverage']['interrupted_tables'] = sorted(set(result['coverage']['present_tables']) - set(result['coverage']['available_tables']))
            else:
                result['coverage']['available_tables'] = []
                result['tables'] = {name: [] for name in FIELDS}
        finally:
            if connection:
                connection.close()
        return result

    @staticmethod
    def _active_failure_projection(recoveries_available=False):
        # Match retrieval freshness without loading source bodies or link rows.
        # Successful/skipped coverage resolves an old failed batch only when no
        # eligible, unsuperseded source in that batch remains uncovered.
        recovery_clause = _terminal_recovery_exclusion('observation_jobs') if recoveries_available else ''
        return f"""CASE WHEN observation_jobs.status = 'failed' {recovery_clause} THEN EXISTS (
            SELECT 1 FROM observation_job_sources s JOIN entries e ON e.id = s.source_id
            WHERE s.job_id = observation_jobs.id AND e.project = observation_jobs.project
                AND e.superseded_by IS NULL
                AND (e.source IN ('hook:UserPromptSubmit','hook:Stop','hook:PostToolUse')
                     OR e.source LIKE 'hook:PostToolUse:%')
                AND NOT EXISTS (
                    SELECT 1 FROM observation_job_sources s2
                    CROSS JOIN observation_jobs j2 ON j2.id = s2.job_id
                    WHERE s2.source_id = e.id AND j2.project = e.project
                        AND j2.status IN ('processed','skipped')))
            ELSE 0 END AS active_failure"""

    @staticmethod
    def _audit_projection():
        # JSON extraction returns only accounting metadata, even when decisions
        # occupy megabytes before the usage fields in the stored receipt.
        fields = ('route', 'model', 'policy_version', 'status', 'usage_status', 'evaluation_source', 'error_code', 'duration_ms', 'generator_started', 'incomplete')
        pairs = []
        for name in fields:
            pairs.extend((f"'{name}'", f"json_extract(audit_json,'$.{name}')"))
        for section, names in (('usage', ('input_tokens', 'output_tokens')), ('counts', ('requests', 'cache_hits', 'evaluated', 'retained', 'discarded', 'chunks'))):
            children = ','.join(f"'{name}',json_extract(audit_json,'$.{section}.{name}')" for name in names)
            pairs.extend((f"'{section}'", f"json_object({children})"))
        return "CASE WHEN json_valid(audit_json) THEN json_object(" + ','.join(pairs) + ") ELSE NULL END AS audit_json"

    @staticmethod
    def _base(snapshot):
        return {'schema_version': 1, 'status': snapshot['status'], 'period': snapshot['period'], 'coverage': snapshot['coverage']}

    @staticmethod
    def _in_period(row, field, snapshot):
        moment = _instant(row.get(field))
        if '_period_bounds' not in snapshot:
            snapshot['_period_bounds'] = (_instant(snapshot['period']['from']), _instant(snapshot['period']['to']))
        start, end = snapshot['_period_bounds']
        return moment is not None and start <= moment < end

    def _report(self, snapshot, project=None, session_id=None, group_by=()):
        with self._lock:
            lock = snapshot.setdefault('_report_lock', threading.RLock())
        with lock:
            report = self._build_report(snapshot, project, session_id, group_by)
        # Derived reports can be shared with a stale snapshot, but freshness
        # metadata belongs to this response, not the cached accounting totals.
        return {**report, 'completeness': {**report['completeness'], 'collection': snapshot['coverage']}}

    def _build_report(self, snapshot, project=None, session_id=None, group_by=()):
        cache_key = (project, session_id, tuple(group_by))
        report_cache = snapshot.setdefault('_reports', {})
        if cache_key in report_cache:
            return report_cache[cache_key]
        tables = snapshot['tables']
        if '_normalized' not in snapshot:
            sessions = {row['thread_id']: row for row in tables['usage_sessions']}
            jobs = {row['id']: row for row in tables['observation_jobs']}
            events = [{**row, **{key: value for key, value in sessions.get(row.get('thread_id'), {}).items() if key != 'session_id'}} for row in tables['usage_events']]
            if snapshot['coverage'].get('legacy_tier_schema'):
                for row in events:
                    row['requested_service_tier'] = row.pop('service_tier', None)
                    row['requested_service_tier_source'] = 'legacy_usage_schema'
            attempts = []
            for row in tables['observer_usage_attempts']:
                job = jobs.get(row.get('job_id'), {})
                source = sessions.get(job.get('session_id'), {})
                if source.get('project') != job.get('project'):
                    source = {}
                attempts.append({**row, **{key: job.get(key) for key in ('project', 'model', 'reasoning_effort')}, 'session_id': source.get('session_id') or job.get('session_id'), 'source_session_id': job.get('session_id'), 'session_attribution_basis': 'usage_session_mapping' if source else 'unmapped_source_session'})
            event_projects, attempt_projects = defaultdict(list), defaultdict(list)
            for row in events:
                event_projects[row.get('project')].append(row)
            for row in attempts:
                attempt_projects[row.get('project')].append(row)
            snapshot['_normalized'] = (events, attempts, event_projects, attempt_projects)
        events, attempts, event_projects, attempt_projects = snapshot['_normalized']
        if project is not None:
            events = event_projects.get(project, [])
            attempts = attempt_projects.get(project, [])
        if session_id is not None:
            events = [row for row in events if row.get('session_id') == session_id]
            attempts = [row for row in attempts if row.get('session_id') == session_id]
        report = build_report(events, attempts, from_date=snapshot['period']['from'], to_date=snapshot['period']['to'], timezone=ZONE, project=project, session_id=session_id, group_by=group_by, max_groups=1000, observer_thread_ids=snapshot.get('_observer_workers', ()), coverage=snapshot['coverage'])
        for stream, required in (('main', ('usage_events', 'usage_sessions')), ('observer', ('observer_usage_attempts', 'observation_jobs'))):
            unavailable = snapshot['status'] == 'unavailable' or not all(table in snapshot['coverage']['available_tables'] for table in required) or (stream == 'main' and snapshot['coverage'].get('observer_workers_complete') is False)
            if unavailable or snapshot['coverage']['truncated_tables']:
                if unavailable:
                    report[stream]['event_count'] = None
                    report['combined']['event_count'] = None
                    for field in TOKEN_FIELDS:
                        report[stream][field] = None
                        report['combined'][field] = None
                for metric in ('api_equivalent_usd', 'estimated_codex_credits'):
                    report[stream][metric]['total'] = None
                    report['combined'][metric]['total'] = None
        if snapshot['coverage']['truncated_tables']:
            report['completeness']['tokens_basis'] = 'retained_rows_lower_bound'
        report_cache[cache_key] = report
        return report

    def _service_records(self, snapshot):
        if '_service_records' not in snapshot:
            records = {}
            try:
                path = self.data_dir / 'service-state.json'
                if path.stat().st_size <= 1024 * 1024:
                    state = json.loads(path.read_text())
                    if isinstance(state.get('projects'), dict):
                        fields = ('due_at', 'attempts', 'blocked', 'parked', 'inflight_until', 'last_code', 'last_failure_at', 'retry_requested', 'rejected_batches', 'last_rejected_reason')
                        records = {key: {field: row.get(field) for field in fields} for key, row in state['projects'].items() if isinstance(row, dict)}
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            snapshot['_service_records'] = records
        return snapshot['_service_records']

    @staticmethod
    def _queue_progress(snapshot, jobs):
        coverage = snapshot['coverage']
        end = _instant(coverage.get('snapshot_at'))
        available = (end is not None and 'observation_jobs' in coverage['available_tables']
                     and 'observation_jobs' not in coverage['truncated_tables']
                     and 'observation_jobs' not in coverage.get('interrupted_tables', ()))
        result = {'available': available, 'window_seconds': 3600,
                  'processed_jobs': None, 'skipped_jobs': None, 'failed_jobs': None,
                  'last_completed_at': None, 'last_attempt_at': None,
                  'basis': 'current_snapshot_last_hour'}
        if not available:
            return result
        start = end - timedelta(seconds=result['window_seconds'])
        counts = Counter()
        last_completed = last_attempt = None
        for row in jobs:
            status = row.get('status')
            if status not in ('processed', 'skipped', 'failed'):
                continue
            completed = _instant(row.get('completed_at')) or _instant(row.get('updated_at'))
            if completed is None or completed > end:
                continue
            last_attempt = max(last_attempt, completed) if last_attempt else completed
            if status in ('processed', 'skipped'):
                last_completed = max(last_completed, completed) if last_completed else completed
            if start <= completed <= end:
                counts[status] += 1
        result.update({status + '_jobs': counts[status] for status in ('processed', 'skipped', 'failed')})
        result['last_completed_at'] = last_completed.isoformat() if last_completed else None
        result['last_attempt_at'] = last_attempt.isoformat() if last_attempt else None
        return result

    def _queue(self, snapshot, project=None):
        jobs = [row for row in snapshot['tables']['observation_jobs'] if project is None or row.get('project') == project]
        counts = Counter(row.get('status') for row in jobs)
        service_records = self._service_records(snapshot)
        records = list(service_records.values()) if project is None else ([service_records[project]] if project in service_records else [])
        pending = snapshot.get('pending_observations')
        pending_count = None if pending is None else (pending.get(project, 0) if project is not None else sum(pending.values()))
        available = 'observation_jobs' in snapshot['coverage']['available_tables']
        failure_available = (available and snapshot['coverage'].get('active_failure_metadata_available', False)
                             and 'observation_jobs' not in snapshot['coverage']['truncated_tables']
                             and 'observation_jobs' not in snapshot['coverage'].get('interrupted_tables', ()))
        active_failures = [row for row in jobs if row.get('status') == 'failed' and row.get('active_failure')]
        oldest = snapshot.get('pending_oldest_at')
        oldest_values = (oldest.get(project),) if oldest is not None and project is not None else (oldest or {}).values()
        pending_oldest = min((value for value in oldest_values if _instant(value) is not None), key=_instant, default=None)
        return {'progress': self._queue_progress(snapshot, jobs), 'pending_oldest_at': pending_oldest, 'queued_projects': len(records), 'blocked_projects': sum(bool(row.get('blocked')) for row in records), 'service_record': service_records.get(project) if project else None, 'pending_observations': pending_count, 'pending_basis': 'Unclaimed raw captures using current processor eligibility, excluding running, completed, skipped and quarantined source snapshots.', 'basis': 'current_snapshot_all_dates', 'available': available, 'pending': pending_count, 'pending_jobs': counts['pending'] if available else None, 'running': counts['running'] if available else None, 'failed': len(active_failures) if failure_available else None, 'quarantined': sum(row.get('error_code') == 'invalid_response' for row in active_failures) if failure_available else None, 'active_failures_available': failure_available, 'failure_basis': 'Failed batches with uncovered eligible unsuperseded raw captures; processed/skipped coverage resolves historical failures.', 'status_counts': dict(counts), 'status_counts_basis': 'Retained job states, including historical failures.'}

    def _capture(self, snapshot, project=None):
        rows = [row for row in snapshot['tables']['entries'] if (project is None or row.get('project') == project) and self._in_period(row, 'created_at', snapshot)]
        available = 'entries' in snapshot['coverage']['available_tables']
        return {'available': available, 'entries': len(rows) if available else None, 'by_kind': dict(Counter(row.get('kind') for row in rows)), 'last_capture_at': max((row.get('created_at') for row in rows if row.get('created_at')), default=None), 'last_note_at': max((row.get('created_at') for row in rows if row.get('kind') in ('observation', 'session_summary', 'note') or row.get('processor_note')), default=None)}

    def _jev(self, snapshot, project=None, job_ids=None):
        jobs = {row['id']: row for row in snapshot['tables']['observation_jobs']}
        streams = {'filter': [], 'quality': [], 'retrieval': []}
        malformed = 0
        retained = Counter()
        for table, timestamp in (('jev_filter_attempts', 'updated_at'), ('jev_judgment_audits', 'created_at')):
            for row in snapshot['tables'][table]:
                owner_project = jobs.get(row.get('job_id'), {}).get('project') if table == 'jev_filter_attempts' else row.get('project')
                if project is not None and owner_project != project:
                    continue
                if job_ids is not None and (row.get('job_id') if table == 'jev_filter_attempts' else row.get('owner_id')) not in job_ids:
                    continue
                if not self._in_period(row, timestamp, snapshot):
                    continue
                if table == 'jev_judgment_audits':
                    retained[owner_project] += 1
                    if retained[owner_project] > 2000:
                        continue
                try:
                    if row.get('audit_json') is None:
                        raise ValueError
                    audit = json.loads(row['audit_json'])
                    if not isinstance(audit, dict):
                        raise ValueError
                except (ValueError, TypeError):
                    malformed += 1
                    continue
                route = 'filter' if table == 'jev_filter_attempts' else audit.get('route')
                if isinstance(route, str) and route.startswith('quality_'):
                    route = 'quality'
                elif isinstance(route, str) and route.startswith('retrieval_'):
                    route = 'retrieval'
                if route not in streams:
                    continue
                # Never return decisions, answers, cache keys or arbitrary JSON.
                safe = {key: audit.get(key) for key in ('model', 'policy_version', 'status', 'usage_status', 'evaluation_source', 'error_code', 'duration_ms', 'generator_started', 'incomplete')}
                safe.update({key: row.get(key) for key in ('job_id', 'attempt_count', 'created_at', 'updated_at') if key in row})
                safe['usage'] = {key: value for key, value in (audit.get('usage') if isinstance(audit.get('usage'), dict) else {}).items() if key in ('input_tokens', 'output_tokens') and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
                safe['counts'] = {key: value for key, value in (audit.get('counts') if isinstance(audit.get('counts'), dict) else {}).items() if key in ('requests', 'cache_hits', 'evaluated', 'retained', 'discarded', 'chunks') and isinstance(value, int) and not isinstance(value, bool) and value >= 0}
                streams[route].append(safe)
        result = {'estimated_usd': None, 'cost_status': 'rate_unavailable', 'malformed_receipts': malformed, 'basis': 'Persistent filter attempts and retained judgment audits. Filter timestamp is latest receipt update; original call time is unavailable. Quality and retrieval retain at most 2000 audits per project and are not lifetime totals.'}
        for route, rows in streams.items():
            result[route] = {'available': ('jev_filter_attempts' if route == 'filter' else 'jev_judgment_audits') in snapshot['coverage']['available_tables'], 'receipts': len(rows), 'input_tokens': sum(row['usage'].get('input_tokens', 0) for row in rows), 'output_tokens': sum(row['usage'].get('output_tokens', 0) for row in rows), 'requests': sum(row['counts'].get('requests', 0) for row in rows), 'cache_hits': sum(row['counts'].get('cache_hits', 0) for row in rows), 'usage_unavailable_receipts': sum(row.get('usage_status') not in ('reported', 'partial') for row in rows), 'attempts': rows[-20:] if job_ids is not None else [], 'coverage': 'persistent_attempts' if route == 'filter' else 'bounded_retained_history'}
            if not result[route]['available']:
                for name in ('receipts', 'input_tokens', 'output_tokens', 'requests', 'cache_hits', 'usage_unavailable_receipts'):
                    result[route][name] = None
        return result

    def overview(self, period='all'):
        snapshot = self._snapshot(period)
        report = self._report(snapshot)
        sizes = {}
        for name in ('memory.sqlite3', 'memory.sqlite3-wal', 'memory.sqlite3-shm'):
            try:
                sizes[name] = (self.data_dir / name).stat().st_size
            except OSError:
                sizes[name] = None
        service = {'status': 'unavailable', 'running': None}
        try:
            path = self.data_dir / 'service-state.json'
            if path.stat().st_size <= 1024 * 1024:
                state = json.loads(path.read_text())
                service.update(status='state_available', stop_requested=state.get('stop_requested'), queued_projects=len(state.get('projects', {})))
                from .service import _read_pid_lock, _pid_lock_held, _pid_alive
                lock = _read_pid_lock(self.data_dir)
                held = _pid_lock_held(self.data_dir)
                owner = state.get('owner')
                service['heartbeat_at'] = None
                service['heartbeat_status'] = 'not_recorded'
                service['state_updated_at'] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                if isinstance(owner, dict):
                    version = owner.get('runtime_version')
                    service['runtime_version'] = version if isinstance(version, str) and len(version) <= 128 else None
                    started = owner.get('started_at')
                    service['started_at'] = started if isinstance(started, (int, float)) and not isinstance(started, bool) else None
                service['lock_held'] = held
                service['running'] = False if held is False else None
                if held is True and lock and isinstance(owner, dict) and owner.get('pid') == lock.get('pid') and owner.get('nonce') == lock.get('nonce'):
                    service['running'] = _pid_alive(lock['pid'])
                    service['pid'] = lock['pid']
                    service['status'] = 'running' if service['running'] else 'stale_owner'
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return {**self._base(snapshot), **{key: report[key] for key in ('main', 'observer', 'combined', 'pricing', 'completeness', 'actual_billing', 'caveats')}, 'storage': {'files_bytes': sizes, 'basis': 'current_local_file_sizes'}, 'capture': self._capture(snapshot), 'queue': self._queue(snapshot), 'service': service, 'jev': self._jev(snapshot)}

    @staticmethod
    def _activity_maps(snapshot):
        if '_activity_maps' in snapshot:
            return snapshot['_activity_maps']
        project_activity, session_activity = {}, {}
        sessions = {row.get('thread_id'): row for row in snapshot['tables']['usage_sessions']}
        def record(project, session, value):
            instant = _instant(value)
            if not project or instant is None:
                return
            stamp = instant.timestamp()
            project_activity[project] = max(stamp, project_activity.get(project, 0))
            if session:
                source = sessions.get(session, {})
                if source.get('project') == project:
                    session = source.get('session_id') or session
                key = (project, session)
                session_activity[key] = max(stamp, session_activity.get(key, 0))
        for table, field in (('entries', 'created_at'), ('observation_jobs', 'updated_at'), ('usage_sessions', 'started_at')):
            for row in snapshot['tables'][table]:
                record(row.get('project'), row.get('session_id'), row.get(field))
        for row in snapshot['tables']['usage_events']:
            source = sessions.get(row.get('thread_id'), {})
            record(source.get('project'), row.get('session_id'), row.get('recorded_at'))
        snapshot['_activity_maps'] = (project_activity, session_activity)
        return snapshot['_activity_maps']

    def projects(self, period='all', page=1, limit=50, query=''):
        if not isinstance(query, str) or len(query) > 500:
            raise ValueError('query must be a string of at most 500 characters')
        snapshot = self._snapshot(period)
        keys = {row.get('project') for table in ('entries', 'usage_sessions', 'observation_jobs') for row in snapshot['tables'][table] if row.get('project')}
        project_activity, _ = self._activity_maps(snapshot)
        keys = sorted((key for key in keys if query.casefold() in key.casefold()), key=lambda key: (-project_activity.get(key, 0), key))
        chosen, pagination = _page(keys, page, limit)
        rows = []
        for key in chosen:
            selected_snapshot = self._snapshot(period, key) if snapshot['coverage']['truncated_tables'] else snapshot
            report = self._report(selected_snapshot, project=key)
            rows.append({'project': key, 'path': key, 'name': Path(key).name or key, 'last_activity_at': datetime.fromtimestamp(project_activity[key], timezone.utc).isoformat() if key in project_activity else None, **{name: report[name] for name in ('main', 'observer', 'combined')}, 'queue': self._queue(selected_snapshot, key), 'capture': self._capture(selected_snapshot, key), 'jev': self._jev(selected_snapshot, key), 'last_error_code': self._service_records(snapshot).get(key, {}).get('last_code'), 'breaker_status': 'blocked' if self._service_records(snapshot).get(key, {}).get('blocked') else 'open', 'next_retry_at': self._service_records(snapshot).get(key, {}).get('due_at'), 'last_progress_at': max((row.get('updated_at') for row in snapshot['tables']['observation_jobs'] if row.get('project') == key and row.get('updated_at')), default=None)})
        return {**self._base(snapshot), 'projects': rows, 'pagination': pagination}

    def project(self, project, period='all', page=1, limit=50):
        key = project_key(project)
        snapshot = self._snapshot(period, key)
        sessions = {row.get('session_id') for table in ('usage_sessions', 'entries', 'observation_jobs') for row in snapshot['tables'][table] if row.get('project') == key and row.get('session_id')}
        # Jobs may point to a child thread rather than the canonical session.
        mapping = {row['thread_id']: row['session_id'] for row in snapshot['tables']['usage_sessions'] if row.get('project') == key}
        _, session_activity = self._activity_maps(snapshot)
        sessions = sorted({mapping.get(value, value) for value in sessions}, key=lambda value: (session_activity.get((key, value), 0), value), reverse=True)
        chosen, pagination = _page(sessions, page, limit)
        report = self._report(snapshot, project=key, group_by=('model',))
        return {**self._base(snapshot), 'project': key, 'path': key, 'name': Path(key).name or key, **{name: report[name] for name in ('main', 'observer', 'combined', 'pricing', 'completeness')}, 'queue': self._queue(snapshot, key), 'capture': self._capture(snapshot, key), 'jev': self._jev(snapshot, key), 'models': {stream: report['groups'][stream]['model']['rows'] for stream in ('main', 'observer')}, 'model_coverage': {stream: {field: report['groups'][stream]['model'][field] for field in ('total_groups', 'omitted_groups', 'limit')} for stream in ('main', 'observer')}, 'sessions': [self._session_summary(snapshot, key, value) for value in chosen], 'pagination': pagination}

    def _session_summary(self, snapshot, project, session_id):
        if '_session_summary_metadata' not in snapshot:
            counts = defaultdict(lambda: {'thread_count': 0, 'job_count': 0, 'job_status_counts': Counter()})
            mapping = {}
            for row in snapshot['tables']['usage_sessions']:
                key = (row.get('project'), row.get('session_id'))
                counts[key]['thread_count'] += 1
                mapping[(row.get('project'), row.get('thread_id'))] = row.get('session_id')
            for row in snapshot['tables']['observation_jobs']:
                source_session = row.get('session_id')
                canonical = mapping.get((row.get('project'), source_session), source_session)
                item = counts[(row.get('project'), canonical)]
                item['job_count'] += 1
                item['job_status_counts'][row.get('status')] += 1
            snapshot['_session_summary_metadata'] = counts
        report = self._report(snapshot, project, session_id)
        counts = snapshot['_session_summary_metadata'].get((project, session_id), {})
        _, activity = self._activity_maps(snapshot)
        moment = activity.get((project, session_id))
        return {'session_id': session_id, 'title': None, 'title_status': 'not_recorded_in_local_usage_metadata', **{name: report[name] for name in ('main', 'observer', 'combined')}, 'thread_count': counts.get('thread_count', 0), 'job_count': counts.get('job_count', 0), 'job_status_counts': dict(counts.get('job_status_counts', {})), 'last_activity_at': datetime.fromtimestamp(moment, timezone.utc).isoformat() if moment is not None else None}

    def session(self, project, session_id, period='all'):
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            raise ValueError('invalid session_id')
        key = project_key(project)
        snapshot = self._snapshot(period, key)
        threads = [row for row in snapshot['tables']['usage_sessions'] if row.get('project') == key and row.get('session_id') == session_id]
        ids = {session_id, *(row['thread_id'] for row in threads)}
        jobs = [row for row in snapshot['tables']['observation_jobs'] if row.get('project') == key and row.get('session_id') in ids]
        job_ids = {row['id'] for row in jobs}
        attempts = [row for row in snapshot['tables']['observer_usage_attempts'] if row.get('job_id') in job_ids and self._in_period(row, 'started_at', snapshot)]
        report = self._report(snapshot, key, session_id, group_by=('agent', 'model'))
        agent_usage = {row['value']: row for row in report['groups']['main']['agent']['rows']}
        thread_models = defaultdict(set)
        for row in snapshot['tables']['usage_events']:
            if row.get('thread_id') in ids and row.get('model') and self._in_period(row, 'recorded_at', snapshot):
                thread_models[row['thread_id']].add(row['model'])
        jobs_by_id = {row['id']: row for row in jobs}
        priced_attempts = []
        for row in attempts:
            job = jobs_by_id[row['job_id']]
            pricing_row = {**row, 'model': job.get('model'), 'model_source': 'requested_job_profile'}
            if row.get('usage_status') not in ('reported', 'partial'):
                pricing_row.update({field: None for field in TOKEN_FIELDS})
            priced_attempts.append({**row, 'model': job.get('model'), 'model_source': 'requested_job_profile', 'priced_usage': price_event(pricing_row)})
        return {**self._base(snapshot), 'project': key, 'session_id': session_id, 'title': None, 'title_status': 'not_recorded_in_local_usage_metadata', **{name: report[name] for name in ('main', 'observer', 'combined', 'pricing', 'completeness')}, 'threads': [{**row, 'usage': agent_usage.get(row['thread_id']), 'models': sorted(thread_models[row['thread_id']]), 'models_basis': 'Observed response metadata within selected period', 'is_child': bool(row.get('parent_thread_id'))} for row in threads], 'jobs': jobs, 'jobs_basis': 'current_state_all_dates', 'models': {stream: report['groups'][stream]['model']['rows'] for stream in ('main', 'observer')}, 'model_coverage': {stream: {field: report['groups'][stream]['model'][field] for field in ('total_groups', 'omitted_groups', 'limit')} for stream in ('main', 'observer')}, 'processor_attempts': priced_attempts, 'failure_receipts': [row for row in snapshot['tables']['observation_failure_receipts'] if row.get('job_id') in job_ids], 'jev': self._jev(snapshot, key, job_ids)}
