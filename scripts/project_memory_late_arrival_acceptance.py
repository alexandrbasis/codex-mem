#!/usr/bin/env python3
"""Inspect or process a frozen report's pending tail in a disposable SQLite clone.

No model is called unless --live is supplied. Processing success is not a semantic
quality verdict or evidence of historical as-of retrieval.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_memory_acceptance as acceptance
from codex_mem.config import configure
from codex_mem.store import Store


def state(data: Path, project: str) -> dict:
    """Read all scoped versions, including superseded episodes, without selection."""
    with closing(sqlite3.connect(f'file:{data / "memory.sqlite3"}?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        def rows(table: str) -> list[dict]:
            return [dict(row) for row in connection.execute(
                f'SELECT * FROM {table} WHERE project=? ORDER BY rowid', (project,))]
        objects = rows('pm_objects')
        episodes = [dict(json.loads(row['data_json']), stored_revision=row['revision'])
                    for row in objects if row['object_type'] == 'episode']
        events = [dict(row) for row in connection.execute(
            'SELECT e.*,s.turn_id,s.created_at AS source_recorded_at FROM pm_events e '
            'JOIN entries s ON s.id=e.source_id WHERE e.project=? ORDER BY e.seq', (project,))]
        return {'episodes': episodes, 'episode_heads': rows('pm_episode_heads'),
                'events': events, 'jobs': rows('pm_jobs'), 'attempts': rows('pm_usage_attempts'),
                'revisions': rows('pm_revisions'), 'projects': rows('pm_projects'),
                'state_fingerprint': acceptance.frozen_state_fingerprint(data, Path(project))}


def run(*, report_path: Path, fixture_path: Path, base_dir: Path,
        live: bool = False, max_jobs: int = 3) -> dict:
    acceptance._bounded_int(max_jobs, 'max_jobs', 1, 3)
    fixture = acceptance.load_fixture(fixture_path)
    fixture_hash = hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest()
    parent = json.loads(report_path.read_text())
    if parent.get('fixture_sha256') != fixture_hash or parent.get('fixture_id') != fixture['fixture_id']:
        raise ValueError('parent report does not match the exact fixture')
    if parent.get('generation_failed') is not False or not parent.get('project_snapshot'):
        raise ValueError('a successful retained project generation report is required')
    cutoff = parent.get('cutoff_arrival_sequence')
    acceptance._bounded_int(cutoff, 'parent cutoff', 0, max(e['sequence'] for e in fixture['events']))
    project = str(Path(parent['project']).resolve())
    source_data = Path(parent['retained_base']) / 'next'
    before_original = state(source_data, project)
    base_dir.mkdir(parents=True, exist_ok=False)
    data = base_dir / 'next'
    data.mkdir()
    with closing(sqlite3.connect(f'file:{source_data / "memory.sqlite3"}?mode=ro', uri=True)) as source, closing(sqlite3.connect(data / 'memory.sqlite3')) as destination:
        source.backup(destination)
    configure(data, capture_scope='all', project_memory_mode='shadow', project_memory_projects=[project])
    before = state(data, project)
    code_start = acceptance.code_fingerprint()
    helper_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    native, receipts = [], []
    started = time.monotonic()
    if live:
        from codex_mem.processor import NativeProcessorRunner
        from codex_mem.episode_processor import process_project_memory
        def capture(request):
            item = {'request': request}
            try:
                item['response'] = NativeProcessorRunner()(request)
                return item['response']
            except Exception as exc:
                item['error'] = {'type': type(exc).__name__, 'code': getattr(exc, 'code', None),
                                 'metrics': getattr(exc, 'metrics', None)}
                raise
            finally:
                native.append(item)
                with (base_dir / 'native-generations.jsonl').open('a') as output:
                    output.write(json.dumps(item, ensure_ascii=False, default=str) + '\n')
        for _ in range(max_jobs):
            with Store(data) as store:
                if not store._connection.execute('SELECT 1 FROM pm_events WHERE project=? AND done=0 LIMIT 1', (project,)).fetchone():
                    break
            result = process_project_memory(Path(project), data, runner=capture)
            receipts.append(result)
            if result.get('status') not in ('processed', 'committed'):
                break
    after = state(data, project)
    original_unchanged = before_original == state(source_data, project)
    if not original_unchanged:
        raise ValueError('original parent state changed during evaluation')
    old_ids = {episode['id'] for episode in before['episodes']}
    new_episodes = [episode for episode in after['episodes'] if episode['id'] not in old_ids]
    known_ids = {episode['id'] for episode in after['episodes']}
    continuations = [episode for episode in new_episodes if episode.get('parent_episode_id') in known_ids]
    by_turn = {event['id']: event for event in acceptance.eligible_events(fixture, cutoff)}
    source_annotations = [{'source_id': event['source_id'], 'fixture_event_id': event['turn_id'],
                           'storage_arrival_sequence': event['seq'],
                           'fixture_arrival_sequence': by_turn.get(event['turn_id'], {}).get('sequence'),
                           'fixture_occurrence_annotation': by_turn.get(event['turn_id'], {}).get('occurred_at'),
                           'source_recorded_at': event['source_recorded_at'],
                           'was_pending_before': not bool(event['done'])}
                          for event in before['events']]
    previous, history = acceptance.prior_attempts([report_path])
    old_attempt_ids = {attempt['attempt_id'] for attempt in before['attempts']}
    return {'schema_version': 1, 'measurement': 'live_late_arrival_ungraded' if live else 'late_arrival_inspection_only',
            'parent_report': str(report_path.resolve()), 'parent_report_sha256': hashlib.sha256(report_path.read_bytes()).hexdigest(),
            'fixture_path': str(fixture_path.resolve()), 'fixture_id': fixture['fixture_id'], 'fixture_sha256': fixture_hash,
            'project': project, 'retained_base': str(base_dir.resolve()), 'max_jobs': max_jobs,
            'versions': {'generation_versions': parent.get('generation_versions') or parent.get('versions'),
                         'code_sha256': code_start, 'code_sha256_at_end': acceptance.code_fingerprint(),
                         'helper_sha256': helper_hash, 'code_changed_during_run': code_start != acceptance.code_fingerprint()},
            'before': before, 'after': after, 'source_annotations': source_annotations,
            'jobs_processed_this_run': receipts, 'native_generations': native,
            'new_attempts': [attempt for attempt in after['attempts'] if attempt['attempt_id'] not in old_attempt_ids],
            'observations': {'new_episode_ids': [episode['id'] for episode in new_episodes],
                             'new_episodes_with_prior_parent': [{'id': e['id'], 'logical_episode_id': e.get('logical_episode_id'),
                                                                'episode_version': e.get('episode_version'), 'parent_episode_id': e.get('parent_episode_id'),
                                                                'parent_existed_before_run': e.get('parent_episode_id') in old_ids}
                                                               for e in continuations],
                             'revision_links_observed': bool(continuations), 'original_state_unchanged': original_unchanged},
            'current_run_observed_usage': acceptance.observed_usage(native),
            'all_linked_attempts_observed_usage': acceptance.observed_usage(previous + native),
            'prior_attempt_history': history, 'cost_usd': None,
            'wall_clock_ms': round((time.monotonic() - started) * 1000),
            'semantic_revision_correctness': None,
            'limitations': ['Occurrence values are fixture annotations, distinct from local capture order and recorded time.',
                            'A processed job or new episode is not proof of correct continuity or semantic revision.',
                            'No historical as-of retrieval or real source event-time guarantee is evaluated.',
                            'No readers or semantic grading are run; inspect receipts, evidence and version links separately.',
                            'At most max_jobs pending jobs are attempted once; no automatic retries.']}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--fixture', type=Path, default=acceptance.FIXTURE)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-jobs', type=int, default=3)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    result = run(report_path=args.report, fixture_path=args.fixture, base_dir=args.base,
                 live=args.live, max_jobs=args.max_jobs)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + '\n')
    print(json.dumps({'output': str(args.output), 'measurement': result['measurement'],
                      'new_attempts': len(result['new_attempts']), 'observations': result['observations']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
