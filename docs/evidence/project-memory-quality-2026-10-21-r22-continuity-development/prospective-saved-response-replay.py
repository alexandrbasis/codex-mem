#!/usr/bin/env python3
"""Disclosed, unminimized response validation under NEW code, never an old trial retry.

Original DB is read-only. Only a temporary backup is opened by Store; no job
is reset, claimed, committed or graded. The entire saved native output is used.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from codex_mem import episode_processor as ep
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store

RUN = Path('/private/tmp/codex-mem-quality-20261021-r21-experiment')


def main():
    paths = [RUN / 'receipts/call-0028-request.json', RUN / 'receipts/call-0028-response.json',
             RUN / 'baseline/h05/data/memory.sqlite3']
    pins = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    request = json.loads(paths[0].read_text())
    original_response = json.loads(paths[1].read_text())
    original_serialized = json.dumps(original_response, sort_keys=True)
    supplied = json.loads(request['prompt'].split('\n', 1)[1])
    snapshot = {key: supplied[key] for key in ('project', 'session_id', 'job_kind', 'reconsideration', 'historical_context')}
    snapshot.update(job_id='pmj_ecbc604fe3aeedab432943f0e31a005d7937b76a',
        revision=supplied['current_state']['revision'], state=supplied['current_state'],
        sources=[{key: value for key, value in event.items() if key != 'spans'} |
                 {'body': ''.join(span['text'] for span in event['spans'])}
                 for event in supplied['events']])
    assert ep._span_catalog(snapshot)[0] == supplied['events']
    output, *_ = ep._validate_runner_receipt(original_response)
    for resolve in (ep._resolve_object_references, ep._resolve_evidence_refs, ep._resolve_quotes):
        output = resolve(output, snapshot)
    output, omissions = ep._omit_unsupported_optional_fields(output)
    ep._validate_output(output, snapshot)
    with tempfile.TemporaryDirectory(prefix='r22-prospective-response-') as scratch:
        original = sqlite3.connect(f'file:{paths[2]}?mode=ro', uri=True)
        before_job = original.execute('SELECT status,error,input_json FROM pm_jobs WHERE id=?', (snapshot['job_id'],)).fetchone()
        assert before_job == ('quarantined', 'invalid_response', '{}')
        target = sqlite3.connect(Path(scratch) / 'memory.sqlite3')
        original.backup(target)
        target.close()
        original.close()
        with Store(scratch) as store:
            before_changes = store._connection.total_changes
            objects, relations, reconciliation = ProjectMemory(store)._validate(snapshot, output, conservative_transitions=True)
            assert store._connection.total_changes == before_changes
            episodes = [item for item in objects if item['object_type'] == 'episode']
            claims = [item for item in objects if item['object_type'] == 'claim']
            assert len(episodes) == 2 and len(claims) == 2 and relations == []
            assert reconciliation['omitted_continuities_count'] == 2
            assert all(item['parent_episode_id'] is None and item['logical_episode_id'] == item['id']
                       and item['episode_version'] == 1 for item in episodes)
            assert store._connection.execute('SELECT status,error,input_json FROM pm_jobs WHERE id=?', (snapshot['job_id'],)).fetchone()[:] == before_job
    assert json.dumps(original_response, sort_keys=True) == original_serialized
    assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == value for path, value in pins.items())
    print(json.dumps({'status': 'prospective_validation_accepts_entire_saved_response',
        'objects': {'standalone_episodes': len(episodes), 'claims': len(claims), 'relations': 0},
        'reconciliation': reconciliation, 'optional_field_omissions': omissions,
        'native_response_unmodified': True, 'original_db_and_receipts_unchanged': True,
        'scratch_validation_writes': 0, 'job_retries': 0, 'commits': 0, 'model_calls': 0,
        'limits': 'Reconstructed disclosed request snapshot; original live-job snapshot erased. Validation only, not a new historical processing result or semantic correctness claim.',
        'source_hashes': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in (Path(ep.__file__), ROOT / 'codex_mem/project_memory.py')},
        'input_pins': pins}, indent=2))


if __name__ == '__main__':
    main()
