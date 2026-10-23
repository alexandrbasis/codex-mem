#!/usr/bin/env python3
"""Create a synthetic dashboard fixture in /tmp; never start a user service.

Run from the checkout: python3 scripts/demo_execution_dashboard.py
Run the printed dashboard command. Stop it with Ctrl-C before cleanup.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shlex
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_mem.config import configure
from codex_mem.store import Store
from codex_mem.usage_store import UsageStore


def create_fixture() -> Path:
    home = Path(tempfile.mkdtemp(prefix='codex-mem-execution-demo-'))
    project = '/synthetic/execution-demo'
    now = datetime.now(timezone.utc)
    stamp = lambda minutes: (now - timedelta(minutes=minutes)).isoformat()
    profile = dict(runtime='pi', provider='anthropic', model='claude-demo', reasoning_effort='low', timeout=120)
    configure(home, service_enabled=False, semantic_enabled=False, processing_profile=profile,
              observation_profile=dict(runtime='codex', provider='openai', model='gpt-5.6-luna', reasoning_effort='low', timeout=120))
    with Store(home) as store:
        # Production schema, synthetic rows. No source body or private receipt.
        from codex_mem.observer_usage_store import _install_schema
        _install_schema(store._connection)
    with UsageStore(home):
        pass
    import sqlite3
    with sqlite3.connect(home / 'memory.sqlite3') as connection:
        def insert(table, row):
            connection.execute(f'INSERT INTO {table} ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
        insert('usage_sessions', dict(thread_id='demo-main', session_id='demo-session', project=project, model_provider='openai', started_at=stamp(60)))
        insert('usage_events', dict(event_key='demo-response', thread_id='demo-main', session_id='demo-session', response_id='demo-response', model='gpt-5.6-luna', model_source='response', model_provider='openai', service_tier='standard', service_tier_source='token_usage_record', recorded_at=stamp(55), source_kind='response', input_tokens=1000, cached_input_tokens=100, cache_write_input_tokens=0, output_tokens=200, reasoning_output_tokens=50, total_tokens=1200))
        for number, runtime, outcome, total in ((1, 'codex', 'processed', 300), (2, 'pi', 'failed', None), (3, 'pi', 'processed', 500)):
            job = f'demo-observation-{number}'
            model = 'gpt-5.6-luna' if runtime == 'codex' else 'claude-demo'
            provider = 'openai' if runtime == 'codex' else 'anthropic'
            insert('observation_jobs', dict(id=job, project=project, session_id='demo-session', processor_id='observation-v1', model=model, reasoning_effort='low', input_fingerprint=job, input_limit=1000, status=outcome, attempt_count=1, output_ids_json='[]', created_at=stamp(50-number), updated_at=stamp(50-number)))
            insert('observer_usage_attempts', dict(job_id=job, attempt_count=1, outcome=outcome, error_code='model_unavailable' if outcome=='failed' else None, runtime=runtime, provider=provider, requested_model=model, reasoning_effort='low', profile_basis='legacy_requested_job_profile' if runtime=='codex' else 'attempt_profile', actual_runtime=runtime if total else None, actual_provider=provider if total else None, actual_model=model if total else None, actual_reasoning_effort='low' if total else None, worker_thread_id='demo-main' if runtime=='pi' else 'demo-codex-worker', usage_status='reported' if total else 'unavailable', usage_source='app_server_thread_total' if runtime=='codex' else 'pi_runtime_total', total_tokens=total, input_tokens=total-50 if total else None, output_tokens=50 if total else None, started_at=stamp(50-number), finished_at=stamp(49-number)))
        from codex_mem.observation_diagnostics import record_failure_receipt
        record_failure_receipt(connection, 'demo-observation-2', 1, 'model_unavailable', 'exact_model_unavailable', stamp(48))
        for number, runtime, outcome, total in ((1, 'codex', 'applied', 400), (2, 'pi', 'failed', None), (3, 'pi', 'applied', 700)):
            model = 'gpt-5.6-luna' if runtime=='codex' else 'claude-demo'
            provider = 'openai' if runtime=='codex' else 'anthropic'
            insert('pm_usage_attempts', dict(attempt_id=f'demo-pm-{number}', project=project, session_id='demo-session', job_id=f'demo-pm-job-{number}', claim_token=f'demo-claim-{number}', processor_id='project-memory-v1', model=model, reasoning_effort='low', requested_runtime=runtime, requested_provider=provider, requested_model=model, requested_reasoning_effort='low', runtime=runtime if total else None, provider=provider if total else None, actual_model=model if total else None, actual_reasoning_effort='low' if total else None, profile_basis='legacy_requested_profile' if runtime=='codex' else 'attempt_profile', worker_thread_id='demo-main' if runtime=='pi' else 'demo-pm-worker', outcome=outcome, error_code='configuration_error' if outcome=='failed' else None, reason_code='unsupported_reasoning' if outcome=='failed' else None, usage_status='reported' if total else 'unknown', usage_source='app_server_thread_total' if runtime=='codex' else 'pi_runtime_total', total_tokens=total, input_tokens=total-70 if total else None, output_tokens=70 if total else None, started_at=stamp(20-number), updated_at=stamp(19-number), finished_at=stamp(19-number)))
    return home


if __name__ == '__main__':
    directory = create_fixture()
    print(json.dumps({'data_dir': str(directory), 'project': '/synthetic/execution-demo',
        'session': 'demo-session', 'start': f'{shlex.quote(sys.executable)} -m codex_mem --data-dir {shlex.quote(str(directory))} ui --port 8766',
        'cleanup': f'rm -rf -- {shlex.quote(str(directory))}'}, indent=2))
