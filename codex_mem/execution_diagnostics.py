"""Content-free profile and attempt diagnostics; reads never dispatch a model."""
from __future__ import annotations

from contextlib import closing
import shutil
import sqlite3
import time

from .config import data_dir_path, load_config
from .execution import PROFILE_KEYS, legacy_profile, resolve_profile
from .observation_diagnostics import safe_failure_reason

ACTIONS = {
    'pi_not_found': 'Install Pi 1.4.0 and make pi available on PATH.',
    'codex_not_found': 'Install Codex and make codex available on PATH.',
    'authentication_unavailable': 'Sign in with the selected runtime native authentication tools, then retry.',
    'unsupported_provider': 'Select a provider supported by the selected runtime and its native configuration.',
    'exact_model_unavailable': 'Select an exact model advertised by the selected provider, then retry.',
    'unsupported_reasoning': 'Choose a reasoning level advertised for the exact model, then retry.',
    'unsupported_pi_version': 'Use the supported Pi version 1.4.0, then retry.',
    'unsupported_codex_protocol': 'Use a Codex version that supports account/read and model/list, then retry.',
    'pi_auth_check_unavailable': 'Use Pi 1.4.0 with its native auth check command, then retry.',
}


def failure_action(code, reason):
    return ACTIONS.get(reason, 'Review the saved failure code and runtime configuration, then retry.' if code else None)


def _attempts(data_dir):
    path = data_dir_path(data_dir) / 'memory.sqlite3'
    if not path.is_file():
        return [], None, 'database_missing'
    attempts, failures = [], []
    deadline = time.monotonic() + 2
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connection.execute('BEGIN')
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, processor in (('observer_usage_attempts', 'observation'), ('pm_usage_attempts', 'project_memory')):
                if table not in tables:
                    continue
                columns = {r[1] for r in connection.execute(f'PRAGMA table_info({table})')}
                fields = ('job_id attempt_id attempt_count model runtime provider requested_runtime requested_provider requested_model '
                          'reasoning_effort requested_reasoning_effort actual_runtime actual_provider actual_model actual_reasoning_effort '
                          'profile_basis worker_thread_id usage_status usage_source total_tokens error_code reason_code outcome started_at finished_at').split()
                select = ','.join('a.' + field for field in fields if field in columns)
                if table == 'observer_usage_attempts' and 'observation_failure_receipts' in tables:
                    select += ',f.reason_code'
                    join = ' LEFT JOIN observation_failure_receipts f ON f.job_id=a.job_id AND f.attempt_count=a.attempt_count'
                else:
                    join = ''
                if table == 'observer_usage_attempts' and 'observation_jobs' in tables:
                    job_columns = {r[1] for r in connection.execute('PRAGMA table_info(observation_jobs)')}
                    for field in ('model', 'reasoning_effort'):
                        if field in job_columns:
                            select += ',j.' + field + ' AS legacy_' + field
                    join += ' LEFT JOIN observation_jobs j ON j.id=a.job_id'
                times = []
                if table == 'observer_usage_attempts' and 'observation_failure_receipts' in tables:
                    times.append(('f.created_at', 'failure_receipt'))
                if 'finished_at' in columns:
                    times.append(('a.finished_at', 'finished_at'))
                times.append(('a.started_at', 'started_at_fallback'))
                timestamp = 'COALESCE(' + ','.join(field for field, _ in times) + ')' if len(times) > 1 else times[0][0]
                basis = 'CASE ' + ' '.join(f"WHEN {field} IS NOT NULL THEN '{name}'" for field, name in times) + ' END'
                select += f', {timestamp} AS failure_recorded_at, {basis} AS failure_time_basis, julianday({timestamp}) AS failure_order'
                for failed in (False, True):
                    query = f'SELECT {select} FROM {table} a{join}'
                    if failed:
                        query += ' WHERE a.error_code IS NOT NULL'
                    query += (' ORDER BY failure_order DESC, a.started_at DESC' if failed else ' ORDER BY a.started_at DESC') + ' LIMIT ?'
                    for raw in connection.execute(query, (1 if failed else 20,)):
                        row = dict(raw)
                        pm = processor == 'project_memory'
                        legacy_model = row.pop('legacy_model', None)
                        legacy_reasoning = row.pop('legacy_reasoning_effort', None)
                        row.update(processor=processor,
                            runtime=row.get('requested_runtime') or row.get('runtime') or 'codex',
                            provider=row.get('requested_provider') or row.get('provider') or 'openai',
                            requested_model=row.get('requested_model') or row.get('model') or legacy_model,
                            reasoning_effort=(row.get('requested_reasoning_effort') or row.get('reasoning_effort') or legacy_reasoning),
                            profile_basis=row.get('profile_basis') or ('legacy_requested_profile' if pm else 'legacy_requested_job_profile'),
                            actual_runtime=raw['runtime'] if pm and 'runtime' in raw.keys() else row.get('actual_runtime'),
                            actual_provider=raw['provider'] if pm and 'provider' in raw.keys() else row.get('actual_provider'),
                            actual_model=row.get('actual_model'), actual_reasoning_effort=row.get('actual_reasoning_effort'))
                        row['reason_code'] = safe_failure_reason(row.get('error_code'), row.get('reason_code'))
                        row['action'] = failure_action(row.get('error_code'), row['reason_code'])
                        (failures if failed else attempts).append(row)
            connection.rollback()
    except (OSError, sqlite3.Error):
        return [], None, 'unavailable'
    attempts.sort(key=lambda row: row.get('started_at') or '', reverse=True)
    failures.sort(key=lambda row: row.get('failure_order') or 0, reverse=True)
    for row in attempts + failures:
        row.pop('failure_order', None)
    return attempts[:20], next(iter(failures), None), 'available'


def execution_status(data_dir=None, *, check=False):
    settings = load_config(data_dir)
    valid = getattr(settings, 'valid', True)
    saved = {key: settings.get(key) if valid else None for key in PROFILE_KEYS}
    result = dict(configuration_valid=valid, default_profile=saved['processing_profile'] or (legacy_profile().to_dict() if valid else None),
                  default_source='processing_profile' if saved['processing_profile'] else 'legacy' if valid else 'invalid',
                  overrides={name: saved[name + '_profile'] for name in ('observation', 'project_memory')}, processors={})
    checked = {}
    for name in ('observation', 'project_memory'):
        if not valid:
            result['processors'][name] = dict(profile=None, source='invalid', availability=dict(status='invalid_configuration', executable=None))
            continue
        profile = resolve_profile(settings, name)
        source = name + '_profile' if saved[name + '_profile'] else 'processing_profile' if saved['processing_profile'] else 'legacy'
        found = shutil.which(profile.runtime) is not None
        availability = dict(executable=found, status='not_checked' if found else 'missing_executable',
                            authentication='not_checked', model='not_checked', reasoning='not_checked', version='not_checked')
        if check:
            key = tuple(profile.to_dict().values())
            if key not in checked:
                checked[key] = check_profile(profile)
            availability.update(checked[key])
        result['processors'][name] = dict(profile=profile.to_dict(), source=source, availability=availability)
    result['latest_attempts'], result['latest_failure'], result['attempts_status'] = _attempts(data_dir)
    return result


def check_profile(profile):
    from .processor import NativeProcessorRunner, ProcessorFailure
    from .pi_runtime import PiProcessorRunner
    try:
        runner = PiProcessorRunner() if profile.runtime == 'pi' else NativeProcessorRunner()
        runner.check_profile(profile, timeout=min(profile.timeout, 10))
        return dict(status='checked', authentication='locally_ready', model='advertised', reasoning='advertised',
                    version='1.4.0' if profile.runtime == 'pi' else 'required_protocol_available',
                    detail='Local preflight passed. Credential validity and provider access are not established by a model call.')
    except ProcessorFailure as error:
        reason = safe_failure_reason(error.code, error.reason_code)
        return dict(status='failed', error_code=error.code, reason_code=reason, action=failure_action(error.code, reason))
    except (OSError, ValueError):
        return dict(status='unavailable', action='Check the selected runtime installation and native configuration.')
