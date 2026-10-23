#!/usr/bin/env python3
"""Opt-in live model smoke at public host adapter boundaries, with disposable state.

This does not establish native interactive hook trust or Pi extension emission.
Only native auth/model files are linked. Credentials and user history are never
read by this helper. No runner, provider, processor, or service seam is replaced.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_mem.config import configure
from codex_mem.hooks import handle_hook
from codex_mem.pi_host import handle_request
from codex_mem.service import enqueue, run_service
from codex_mem.store import Store


def passed(report):
    profile = report['profile']
    return (report['initial_entry_count'] == 0 and report['source_count'] == 1 and report['enqueue_status'] == 'queued'
        and report['queue_file_created'] and report['next_request_memory_contains_fact']
        and report['generated_note_count'] > 0 and report['generated_note_delivered']
        and report['generated_outputs_distinct_from_sources'] and report['generated_source_project_refs_valid']
        and any(row['status'] == 'processed' for row in report['jobs'])
        and any(attempt.get('outcome') == 'processed'
            and all(attempt.get('actual_' + key) == profile[key]
                    for key in ('runtime', 'provider', 'model', 'reasoning_effort'))
            for attempt in report['attempts']))


@contextmanager
def environment(values):
    previous = dict(os.environ)
    os.environ.update(values)
    os.environ.pop('CODEX_MEM_DISABLED', None)
    os.environ.pop('PI_CODING_AGENT_SESSION_DIR', None)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def pair(host, runtime, *, timeout, native_pi, native_codex, executables):
    profile = dict(runtime=runtime, provider='openai-codex' if runtime == 'pi' else 'openai',
                   model='gpt-6-luna', reasoning_effort='low', timeout=timeout)
    report = dict(host=host, runtime=runtime, profile=profile, status='unpassed',
                  boundary='public_adapter_synthetic_host_event',
                  native_interactive_emission='not_tested', model_process='real_native_remote')
    with tempfile.TemporaryDirectory(prefix='codex-mem-live-matrix-') as temporary:
        root = Path(temporary).resolve()
        for name in ('home', 'pi', 'codex', 'project', 'bin'):
            (root / name).mkdir()
        for filename in ('auth.json', 'models.json'):
            if (native_pi / filename).is_file():
                (root / 'pi' / filename).symlink_to(native_pi / filename)
        if (native_codex / 'auth.json').is_file():
            (root / 'codex/auth.json').symlink_to(native_codex / 'auth.json')
        for name, executable in executables.items():
            if executable and not (host == runtime == 'pi' and name == 'codex'):
                (root / 'bin' / name).symlink_to(executable)
        values = dict(HOME=str(root / 'home'), CODEX_HOME=str(root / 'codex'),
                      PI_CODING_AGENT_DIR=str(root / 'pi'), CODEX_MEM_HOME=str(root / 'memory'),
                      PATH=str(root / 'bin'), NO_COLOR='1')
        with environment(values):
            project, data = root / 'project', root / 'memory'
            report['versions'] = {}
            for name in ('pi', 'codex'):
                if shutil.which(name):
                    version = subprocess.run([name, '--version'], cwd=project,
                        capture_output=True, text=True, timeout=10)
                    report['versions'][name] = version.stdout.strip()[:80] if version.returncode == 0 else 'unavailable'
            report['codex_executable_available'] = shutil.which('codex') is not None
            if host == runtime == 'pi' and report['codex_executable_available']:
                raise RuntimeError('Pi/Pi must exclude the Codex executable')
            configure(data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                      usage_enabled=False, processing_profile=profile, processor_workers=1)
            text = ('IMPORTANT: Confirmed decision for the synthetic Orchid project. '
                    'The invoice export must use CSV format and the delimiter must be a semicolon. '
                    'Keep this requirement for future invoice export work.')
            with Store(data) as store:
                report['initial_entry_count'] = len(store.timeline(project, limit=20))
                if host == 'codex':
                    capture = handle_hook(dict(hook_event_name='UserPromptSubmit', cwd=str(project),
                        session_id='live-writer', turn_id='live-turn', message_id='live-message', prompt=text), store)
                else:
                    capture = handle_request(dict(version=1, pi_version='1.4.0', operation='capture',
                        project=str(project), session_id='live-writer', settled=True, events=[
                            dict(role='user', message_id='live-message', text=text)]), store)
                sources = store.timeline(project, limit=20)
                source_ids = {entry['id'] for entry in sources}
                report['source_count'] = len(sources)
            report['capture_status'] = capture.get('status', 'hook_returned')
            configure(data, service_enabled=True)
            report['enqueue_status'] = enqueue(project, data).get('status')
            report['queue_file_created'] = (data / 'service-state.json').is_file()
            started = time.monotonic()
            service = run_service(data, max_cycles=1, poll_interval=0, max_timeout_retries=0,
                                  processor_timeout=timeout, processor_workers=1)
            report['elapsed_seconds'] = round(time.monotonic() - started, 2)
            report['service_status'] = service.get('status')
            with Store(data) as store:
                with sqlite3.connect(store.db_path) as connection:
                    connection.row_factory = sqlite3.Row
                    jobs = connection.execute('SELECT id,status,error_code FROM observation_jobs').fetchall()
                report['jobs'] = [dict(row) for row in jobs]
                output_ids = []
                for row in jobs:
                    output_ids.extend(store.observation_job_status(project, row['id'])['output_ids'])
                generated = store.get(project, output_ids) if output_ids else []
                report['generated_note_count'] = len(generated)
                report['attempts'] = []
                for row in jobs:
                    for attempt in store.observation_attempts(project, row['id']):
                        saved = {key: attempt.get(key) for key in (
                            'runtime', 'provider', 'reasoning_effort',
                            'requested_runtime', 'requested_provider', 'requested_model',
                            'requested_reasoning_effort', 'actual_runtime', 'actual_provider', 'actual_model',
                            'actual_reasoning_effort', 'outcome', 'error_code', 'reason_code',
                            'total_tokens', 'usage_status')}
                        receipt = json.loads(attempt.get('execution_receipt') or '{}')
                        saved['execution_receipt'] = {key: receipt.get(key) for key in (
                            'runtime', 'version', 'protocol', 'generation', 'settled', 'identity_verified',
                            'tool_calls_observed', 'state_checked', 'agent_end_observed',
                            'system_message_verified', 'assistant_stop_reason') if key in receipt}
                        report['attempts'].append(saved)
                if host == 'codex':
                    context = handle_hook(dict(hook_event_name='SessionStart', source='startup',
                        cwd=str(project), session_id='live-reader'), store)
                else:
                    context = handle_request(dict(version=1, pi_version='1.4.0', operation='context',
                        project=str(project), session_id='live-reader', phase='start', query='invoice export'), store)
                serialized = json.dumps(context).lower()
                report['next_request_memory_contains_fact'] = 'semicolon' in serialized
                report['generated_note_delivered'] = any(
                    note['id'].lower() in serialized and 'semicolon' in note['body'].lower()
                    and note['source'].startswith('processor:') for note in generated)
                report['generated_outputs_distinct_from_sources'] = all(
                    note['id'] not in note.get('source_ids', []) for note in generated)
                report['generated_source_project_refs_valid'] = bool(generated) and all(
                    note['project'] == str(project) and set(note.get('source_ids', [])) == source_ids
                    for note in generated)
                if passed(report):
                    report['status'] = 'passed'
    report['temporary_state_removed'] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Authorize real bounded model calls on synthetic input')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=60)
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required')
    if not 5 <= args.timeout <= 180:
        parser.error('--timeout must be 5..180 seconds')
    native_pi = Path(os.environ.get('PI_CODING_AGENT_DIR', Path.home() / '.pi/agent')).expanduser().resolve()
    native_codex = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')).expanduser().resolve()
    executables = {name: shutil.which(name) for name in ('pi', 'codex', 'node', 'python3', 'ps')}
    report = dict(schema_version=1, measurement='live_public_host_runtime_matrix',
                  repository_revision=subprocess.run(['git', 'rev-parse', 'HEAD'],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                    check=True, timeout=5).stdout.strip(),
                  environment_boundary=dict(home='temporary', codex_home='temporary_native_auth_link_only',
                    pi_agent_dir='temporary_native_auth_and_models_links_only',
                    projects_and_store='temporary_per_pair', network='inherited_from_invocation',
                    user_history='not_read', global_configuration='not_modified',
                    pi_pi_path='pi_node_python3_ps_only_no_codex'),
                  captured_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), pairs=[])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for host, runtime in (('codex', 'pi'), ('pi', 'pi'), ('codex', 'codex'), ('pi', 'codex')):
        print(f'Live smoke {host}/{runtime}', file=sys.stderr, flush=True)
        try:
            result = pair(host, runtime, timeout=args.timeout, native_pi=native_pi,
                          native_codex=native_codex, executables=executables)
        except Exception as error:
            result = dict(host=host, runtime=runtime, status='unpassed', failure_type=type(error).__name__)
        report['pairs'].append(result)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    return 0 if all(row['status'] == 'passed' for row in report['pairs']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
