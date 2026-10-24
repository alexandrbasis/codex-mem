#!/usr/bin/env python3
"""Opt-in bounded comparison. Only synthetic fixtures enter isolated real processors.

The recorder observes the public execution contract without changing its result.
Invalid raw model text is unavailable at this boundary by design. Native adapters
remove it before returning a failure. Never scrape native auth or session logs.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_mem.config import configure
from codex_mem.episode_processor import process_project_memory
from codex_mem.host import source_role
from codex_mem.pi_host import handle_request
from codex_mem.pi_runtime import PiProcessorRunner
from codex_mem.processor import NativeProcessorRunner, process_pending
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store
from scripts.live_host_runtime_smoke import environment

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/memory_profile_comparison.json'


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Recorder:
    """Record one execution without interpreting or changing its output."""
    def __init__(self, runner, destination):
        self.runner, self.destination, self.result = runner, destination, None
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        if self.calls != 1:
            raise RuntimeError('comparison permits one execution per public invocation')
        write(self.destination / 'request.json', asdict(request))
        started = time.monotonic()
        result = self.runner.execute(request)
        self.result = asdict(result)
        write(self.destination / 'execution.json', self.result)
        write(self.destination / 'timing.json', {'seconds': time.monotonic() - started})
        return result


def measure(project, data, kind, destination, runner, *, timeout=60):
    destination.mkdir(parents=True, exist_ok=False)
    recorder = Recorder(runner, destination)
    with Store(data) as store:
        before = {row['id'] for row in store.timeline(project, limit=100)}
    started = time.monotonic()
    function = process_project_memory if kind == 'project_memory' else process_pending
    options = {} if kind == 'project_memory' else {
        'summary_reviewer': Recorder(runner, destination / 'summary-support')}
    receipt = function(project, data, runner=recorder, timeout=timeout, **options)
    with Store(data) as store:
        rows = store.timeline(project, limit=100)
        sources = [dict(row, evidence_role=source_role(row.get('source'))) for row in rows
                   if source_role(row.get('source')) in ('user_intent', 'assistant_report', 'tool_record')]
        if kind == 'project_memory':
            memory = ProjectMemory(store)
            generated = memory.snapshot(project)
            attempts = memory.attempts(project)
            job = None
        else:
            job = store.observation_job_status(project, receipt['job_id']) if receipt.get('job_id') else None
            attempts = store.observation_attempts(project, receipt['job_id']) if receipt.get('job_id') else []
            generated = [row for row in rows if row['id'] not in before and row.get('source', '').startswith('processor:')]
        context = handle_request(dict(version=1, pi_version='1.4.0', operation='context',
            project=str(project), session_id='reader', phase='start', query='requirements status evidence'), store)
    report = dict(kind=kind, receipt=receipt, seconds=time.monotonic() - started,
        execution=recorder.result, execution_calls=recorder.calls, attempts=attempts, job=job,
        sources=sources, generated=generated, context=context,
        raw_boundary='Parsed original output in execution.json; invalid raw text is not exposed by native adapters. No native logs read.')
    write(destination / 'result.json', report)
    return report


def capture(project, data, events):
    with Store(data) as store:
        result = handle_request(dict(version=1, pi_version='1.4.0', operation='capture',
            project=str(project), session_id='quality', settled=True, events=events), store)
    if result.get('status') != 'ok' or any(e.get('status') != 'captured' for e in result.get('events', [])):
        raise RuntimeError('synthetic capture failed')
    return result


def isolated(root, native, executables):
    for name in ('home', 'pi', 'codex', 'project', 'other', 'bin'):
        (root / name).mkdir()
    for runtime, names in (('pi', ('auth.json', 'models.json')), ('codex', ('auth.json',))):
        for name in names:
            source = native[runtime] / name
            if source.is_file():
                (root / runtime / name).symlink_to(source)
    for name, executable in executables.items():
        if executable:
            (root / 'bin' / name).symlink_to(executable)
    return dict(HOME=str(root / 'home'), CODEX_HOME=str(root / 'codex'),
        PI_CODING_AGENT_DIR=str(root / 'pi'), PATH=str(root / 'bin'), NO_COLOR='1')


def freeze(destination, fixture, versions):
    paths = [Path(__file__), FIXTURE, ROOT / 'codex_mem/processor.py', ROOT / 'codex_mem/pi_runtime.py',
             ROOT / 'codex_mem/episode_processor.py', ROOT / 'codex_mem/execution.py']
    manifest = dict(created_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        baseline=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        versions=versions, design=fixture, files={str(p.relative_to(ROOT)): digest(p) for p in paths})
    write(destination / 'freeze.json', manifest)
    for path in paths:
        target = destination / 'frozen' / path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if args.prepare == args.live:
        parser.error('select exactly one of --prepare or --live')
    fixture = json.loads(FIXTURE.read_text())
    native = {'codex': Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')),
              'pi': Path(os.environ.get('PI_CODING_AGENT_DIR', Path.home() / '.pi/agent'))}
    executables = {name: shutil.which(name) for name in ('codex', 'pi', 'node', 'python3', 'ps')}
    if args.prepare:
        args.output.mkdir(parents=True, exist_ok=False)
        with tempfile.TemporaryDirectory(prefix='memory-quality-versions-') as temporary:
            root = Path(temporary).resolve()
            values = isolated(root, native, executables)
            with environment(values):
                versions = {name: subprocess.check_output([name, '--version'], cwd=root / 'project', text=True, timeout=10).strip()
                            for name in ('codex', 'pi')}
        freeze(args.output, fixture, versions)
        print(digest(args.output / 'freeze.json'))
        return
    manifest = json.loads((args.output / 'freeze.json').read_text())
    for name, expected in manifest['files'].items():
        if digest(ROOT / name) != expected:
            raise RuntimeError('frozen input changed')
    if fixture != manifest['design']:
        raise RuntimeError('design changed')
    # Exclusive marker prevents accidental repeat or silent resume of a partial batch.
    (args.output / 'started.json').open('x').write(json.dumps({'started_at': time.time()}))
    index = []
    for case in fixture['cases']:
        for profile in fixture['profiles']:
            for group in ('observation_summary', 'project_memory'):
                with tempfile.TemporaryDirectory(prefix='memory-quality-') as temporary:
                    root = Path(temporary).resolve()
                    values = isolated(root, native, executables)
                    with environment(values):
                        versions = {name: subprocess.check_output([name, '--version'], cwd=root / 'project', text=True, timeout=10).strip()
                                    for name in ('codex', 'pi')}
                        if versions != manifest['versions']:
                            raise RuntimeError('native version changed')
                        project, data = root / 'project', root / 'memory'
                        configure(data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                            usage_enabled=False, processing_profile=profile, project_memory_mode='pilot',
                            project_memory_projects=[str(project)])
                        capture(root / 'other', data, [dict(role='user', message_id='other', text=case['distractor'])])
                        stages = [('project_memory', case['initial'] + case['continuation'])] if group == 'project_memory' else [
                            ('observation', case['initial']), ('summary', case['continuation'])]
                        for kind, events in stages:
                            label = f"{case['id']}-{profile['runtime']}-{kind}"
                            captured = capture(project, data, events)
                            runner = PiProcessorRunner() if profile['runtime'] == 'pi' else NativeProcessorRunner()
                            destination = args.output / 'runs' / label
                            report = measure(project, data, kind, destination, runner, timeout=profile['timeout'])
                            write(destination / 'capture.json', captured)
                            index.append(dict(cell=label, kind=kind, status=report['receipt']['status'],
                                result_sha256=digest(destination / 'result.json')))
                            write(args.output / 'index.json', index)
                            print(label, report['receipt']['status'], flush=True)
    write(args.output / 'completed.json', {'cells': len(index), 'completed_at': time.time()})


if __name__ == '__main__':
    main()
