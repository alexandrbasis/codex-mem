#!/usr/bin/env python3
"""Opt-in installed daemon/MCP probe using only a temporary synthetic store.

Hook inputs use the packaged hook entrypoint and native payload shapes. This
checks that adapter boundary, not that the Codex host emitted/trusted hooks.
Without --live, no hooks, models, daemon, or configuration writes run.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any


class AcceptanceError(RuntimeError):
    pass


def require(condition: Any, code: str) -> None:
    if not condition:
        raise AcceptanceError(code)


def content(result: dict) -> Any:
    require(not result.get('isError'), 'mcp_tool_failed')
    if 'structuredContent' in result:
        structured = result['structuredContent']
        # MCP requires structuredContent to be an object. The installed server
        # wraps list results, including memory_get, in {"result": [...]}.
        if isinstance(structured, dict) and set(structured) == {'result'}:
            return structured['result']
        return structured
    for block in result.get('content', []):
        if block.get('type') == 'text':
            return json.loads(block['text'])
    raise AcceptanceError('mcp_content_missing')


class MCP:
    def __init__(self, launcher: Path, env: dict[str, str], timeout: float = 30):
        self.child = subprocess.Popen([sys.executable, str(launcher), 'serve'], env=env,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, bufsize=0)
        self.timeout = timeout
        self.sequence = 0
        self.buffer = b''

    def send(self, method: str, params: dict, *, notification: bool = False) -> Any:
        self.sequence += 1
        message = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notification:
            message['id'] = self.sequence
        assert self.child.stdin is not None and self.child.stdout is not None
        self.child.stdin.write(json.dumps(message).encode() + b'\n')
        if notification:
            return None
        deadline = time.monotonic() + self.timeout
        while True:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                response = json.loads(line)
                if response.get('id') == self.sequence:
                    require('error' not in response, 'mcp_protocol_error')
                    return response['result']
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'mcp_timeout')
            readable, _, _ = select.select([self.child.stdout], [], [], remaining)
            require(readable, 'mcp_timeout')
            chunk = os.read(self.child.stdout.fileno(), 65536)
            require(chunk, 'mcp_closed')
            self.buffer += chunk
            require(len(self.buffer) <= 2 * 1024 * 1024, 'mcp_result_limit')

    def call(self, name: str, arguments: dict) -> Any:
        return content(self.send('tools/call', {'name': name, 'arguments': arguments}))

    def close(self) -> None:
        if self.child.stdin:
            self.child.stdin.close()
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.child.terminate()
            self.child.wait(timeout=5)
        if self.child.stdout:
            self.child.stdout.close()


def rows(data: Path, sql: str, params: tuple = ()) -> list[dict]:
    database = data / 'memory.sqlite3'
    if not database.exists():
        # Derive the filename from this isolated directory, never a global home.
        candidates = list(data.glob('*.db')) + list(data.glob('*.sqlite*'))
        require(len(candidates) == 1, 'temporary_database_missing')
        database = candidates[0]
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, params)]


def run(plugin: Path, *, live: bool, timeout: int) -> dict:
    launcher = plugin / 'scripts/codex-mem.py'
    manifest_path = plugin / '.codex-plugin/plugin.json'
    require(launcher.is_file() and manifest_path.is_file(), 'installed_plugin_files_missing')
    manifest = json.loads(manifest_path.read_text())
    receipt: dict[str, Any] = {'schema_version': 1, 'measurement': 'installed_daemon_mcp_runtime',
        'live': live, 'installed_manifest_version': manifest.get('version'),
        'capture_boundary': 'packaged_hook_with_synthetic_native_shaped_payloads',
        'native_host_hook_trust': 'not_tested', 'answer_quality': 'not_measured', 'checks': {}}
    if not live:
        return dict(receipt, status='not_run', reason='requires_explicit_live')
    base = Path(tempfile.mkdtemp(prefix='codex-mem-project-runtime-')).resolve()
    try:
        project = base / 'orchid'
        project.mkdir()
        data = base / 'memory'
        env = dict(os.environ, CODEX_MEM_HOME=str(data))
        env.pop('CODEX_MEM_DISABLED', None)
        env.pop('PYTHONPATH', None)
        mcp = None
        service_owner = None

        def cli(*args: str, payload: dict | None = None, limit: int = 30) -> Any:
            result = subprocess.run([sys.executable, str(launcher), *args], env=env,
                input=json.dumps(payload) if payload is not None else None,
                capture_output=True, text=True, timeout=limit)
            require(result.returncode == 0, 'installed_command_failed_' + args[0])
            return json.loads(result.stdout) if result.stdout.strip() else {}

        def hook(event: str, session: str, **payload: Any) -> None:
            cli('hook', payload={'hook_event_name': event, 'cwd': str(project),
                'session_id': session, 'turn_id': 'synthetic-turn', **payload})

        try:
            configuration = cli('config', '--scope', 'selected', '--include-project', str(project),
                '--project-memory-mode', 'pilot', '--project-memory-project', str(project),
                '--no-semantic-enabled', '--no-service-enabled', '--no-processor-enabled', '--no-usage-enabled')
            require(configuration['project_memory_projects'] == [str(project)], 'exact_scope_not_configured')
            primary = 'runtime-no-stop'
            hook('UserPromptSubmit', primary, prompt='IMPORTANT: Orchid must keep production deployment open until a separate production check. Local validation is not production verification.')
            script = project / 'regression.py'
            script.write_text("assert len(set(['checkout-17', 'checkout-17'])) == 1\nprint('Orchid local duplicate regression passed. Production deployment was not performed or verified.')\n")
            actual = subprocess.run([sys.executable, str(script)], cwd=project, text=True,
                                    capture_output=True, check=True, timeout=10)
            hook('PostToolUse', primary, tool_name='exec_command', tool_use_id='runtime-local-regression',
                 tool_input={'cmd': 'python3 regression.py'},
                 tool_response={'exit_code': actual.returncode, 'output': actual.stdout})
            captured = rows(data, 'SELECT id,project,session_id,source FROM entries ORDER BY created_at')
            primary_sources = {row['id'] for row in captured if row['session_id'] == primary}
            require(len(primary_sources) == 2, 'primary_hook_capture_missing')
            require(not any(row['source'] == 'hook:Stop' for row in captured if row['session_id'] == primary), 'unexpected_primary_stop')
            require(not (data / 'service-state.json').exists(), 'hook_started_service_despite_configuration')
            receipt['checks'].update(capture_committed=True, primary_no_stop=True,
                                     no_explicit_enqueue=True, synthetic_tool_executed=True)
            cli('config', '--service-enabled', '--processor-enabled')
            started = time.monotonic()
            receipt['service_start'] = cli('service', 'start')
            status = cli('service', 'status')
            service_owner = status.get('owner_id')
            require(service_owner, 'temporary_service_owner_missing')
            receipt['runtime_version'] = status.get('runtime_version')
            require(receipt['runtime_version'] == manifest.get('version'), 'daemon_version_mismatch')
            print(json.dumps({'phase': 'temporary_daemon_started', 'captured_sources': len(primary_sources)}),
                  file=sys.stderr, flush=True)
            deadline = time.monotonic() + timeout
            next_progress = time.monotonic() + 30
            last_jobs = []
            while time.monotonic() < deadline:
                jobs = rows(data, 'SELECT id,session_id,status,error,attempts,receipt_json FROM pm_jobs')
                last_jobs = [{**{key: job[key] for key in ('id', 'session_id', 'status', 'error', 'attempts')},
                              'receipt': json.loads(job['receipt_json'] or '{}')} for job in jobs]
                if any(job['status'] == 'quarantined' for job in jobs):
                    receipt['jobs'] = last_jobs
                    raise AcceptanceError('native_job_quarantined')
                successful = [job for job in jobs if job['session_id'] == primary and job['status'] == 'completed']
                if successful:
                    receipt['native_receipt'] = json.loads(successful[-1]['receipt_json'] or '{}')
                    break
                if time.monotonic() >= next_progress:
                    print(json.dumps({'phase': 'waiting_for_native_project_memory',
                                      'jobs': [{key: job[key] for key in ('status', 'error', 'attempts')} for job in jobs]}),
                          file=sys.stderr, flush=True)
                    next_progress = time.monotonic() + 30
                time.sleep(1)
            else:
                receipt['jobs'] = last_jobs
                raise AcceptanceError('durable_daemon_processing_timeout')
            receipt['processing_seconds'] = round(time.monotonic() - started, 2)
            receipt['jobs'] = last_jobs
            processing = receipt['native_receipt'].get('processing', {})
            require(processing.get('model') and processing.get('worker_thread_id') and
                    processing.get('worker_turn_id'), 'native_generation_profile_missing')
            mcp = MCP(launcher, env)
            initialized = mcp.send('initialize', {'protocolVersion': '2025-11-25',
                'capabilities': {}, 'clientInfo': {'name': 'project-memory-runtime-acceptance', 'version': '1'}})
            mcp.send('notifications/initialized', {}, notification=True)
            require(initialized['serverInfo']['version'] == manifest.get('version'), 'mcp_version_mismatch')
            tools = mcp.send('tools/list', {})['tools']
            require(any(tool['name'] == 'memory_project_state' and tool['annotations']['readOnlyHint'] for tool in tools), 'installed_state_tool_missing')
            brief = mcp.call('memory_project_state', {'project': str(project), 'budget': 6000})
            episodes = [record for record in brief['episodes'] if primary_sources.intersection(record.get('source_ids', []))]
            require(brief['enabled'] and brief['mode'] == 'pilot', 'pilot_state_not_enabled')
            require(episodes, 'source_backed_primary_episode_missing')
            require(all(record.get('project') == str(project) for record in brief['records']), 'foreign_project_record')
            source_ids = list(dict.fromkeys(source for record in episodes for source in record.get('source_ids', [])))
            fetched = mcp.call('memory_get', {'project': str(project), 'ids': source_ids})
            require(isinstance(fetched, list) and primary_sources.intersection(record['id'] for record in fetched), 'mcp_source_readback_missing')
            derived = mcp.call('memory_get', {'project': str(project), 'ids': [episodes[0]['id']]})
            require(derived and derived[0]['id'] == episodes[0]['id'], 'mcp_derived_readback_missing')
            require(derived[0].get('verification') not in ('verified', True), 'unsupported_verification_claim')
            bodies = {record['id']: record['body'] for record in fetched}
            roles = {row['id']: 'user_intent' if row['source'] == 'hook:UserPromptSubmit' else 'tool_record'
                     for row in captured}
            evidence_count = 0
            for refs in derived[0].get('evidence', {}).values():
                for ref in refs:
                    require(ref['source_id'] in bodies, 'derived_source_unreadable')
                    require(ref.get('source_role') == roles.get(ref['source_id']), 'source_role_mismatch')
                    require(bodies[ref['source_id']][ref['start']:ref['end']] == ref['quote'], 'evidence_span_mismatch')
                    evidence_count += 1
            require(evidence_count, 'derived_evidence_missing')
            foreign = base / 'other-project'
            foreign.mkdir()
            other = mcp.call('memory_project_state', {'project': str(foreign)})
            require(not other['enabled'] and not other['records'], 'scope_isolation_failed')
            require(all(record.get('evidence') for record in episodes), 'episode_provenance_missing')
            receipt['checks'].update(durable_no_stop_discovery=True, native_generation=True,
                installed_mcp_version=True, state_tool=True, source_readback=True,
                derived_readback=True, exact_project_scope=True, episode_provenance=True)
            receipt['synthetic_brief'] = brief
            receipt['status'] = 'passed'
        except Exception as exc:
            receipt.update(status='failed', error=exc.args[0] if isinstance(exc, AcceptanceError) else type(exc).__name__)
        finally:
            if mcp:
                try:
                    mcp.close()
                except Exception as exc:
                    receipt.update(status='failed', mcp_cleanup_error=type(exc).__name__)
            try:
                status = cli('service', 'status')
                owner = status.get('owner_id')
                require(not owner or service_owner is None or owner == service_owner, 'temporary_service_owner_changed')
                if owner and (service_owner is None or owner == service_owner):
                    stopped = cli('service', 'stop', '--expected-owner', owner)
                    receipt['service_stop'] = stopped
                    stop_deadline = time.monotonic() + 300
                    while cli('service', 'status').get('running') and time.monotonic() < stop_deadline:
                        time.sleep(0.2)
                    require(cli('service', 'status').get('status') == 'stopped', 'temporary_service_stop_timeout')
                elif status.get('status') == 'starting':
                    # The process is confined to this probe's fresh directory.
                    # A durable stop also covers a child not yet registered.
                    receipt['service_stop'] = cli('service', 'stop')
                    stop_deadline = time.monotonic() + 300
                    while time.monotonic() < stop_deadline:
                        status = cli('service', 'status')
                        if status.get('status') == 'stopped':
                            break
                        time.sleep(0.2)
                    require(status.get('status') == 'stopped', 'temporary_service_stop_timeout')
                else:
                    require(status.get('status') == 'stopped', 'temporary_service_status_unconfirmed')
                receipt['temporary_service_stopped'] = True
            except Exception as exc:
                receipt['temporary_service_stopped'] = False
                receipt['cleanup_error'] = exc.args[0] if isinstance(exc, AcceptanceError) else type(exc).__name__
                receipt['status'] = 'failed'
    finally:
        if receipt.get('temporary_service_stopped'):
            try:
                shutil.rmtree(base)
            except OSError:
                receipt.update(status='failed', cleanup_error='temporary_directory_cleanup_failed',
                               retained_fixture=str(base))
        else:
            # Preserve the worker's files if shutdown is unknown. Never delete
            # an active daemon's store and claim successful cleanup.
            receipt['retained_fixture'] = str(base)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    require(30 <= args.timeout <= 1200, 'timeout_out_of_range')
    try:
        result = run(args.plugin_root.expanduser().resolve(), live=args.live, timeout=args.timeout)
    except Exception as exc:
        result = {'schema_version': 1, 'status': 'failed', 'live': args.live,
                  'measurement': 'installed_daemon_mcp_runtime',
                  'error': exc.args[0] if isinstance(exc, AcceptanceError) else type(exc).__name__}
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({key: result.get(key) for key in ('status', 'live', 'measurement', 'error')}))
    return 2 if result['status'] == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
