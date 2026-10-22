#!/usr/bin/env python3
"""Offline diagnosis only. Original SQLite opened mode=ro; validation uses a scratch backup.

--red returns 1 on the reproduced rejection, without committing or retrying anything.
Default runs discriminating counterfactuals, never repairs the historical response.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
import jsonschema

RUN = Path('/private/tmp/codex-mem-quality-20261021-r21-experiment')
JOB = 'pmj_ecbc604fe3aeedab432943f0e31a005d7937b76a'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=RUN)
    parser.add_argument('--runtime-root', type=Path, default=ROOT)
    parser.add_argument('--red', action='store_true')
    args = parser.parse_args()
    frozen = json.loads((ROOT / 'docs/evidence/project-memory-quality-2026-10-21-r21/runtime-source-receipt.json').read_text())
    assert all(digest(args.runtime_root / path) == value for path, value in frozen['runtime_files'].items()), 'runtime differs from frozen R21'
    sys.path.insert(0, str(args.runtime_root))
    from codex_mem import episode_processor as ep
    from codex_mem.project_memory import ProjectMemory
    from codex_mem.store import Store
    run = args.run
    request_path = run / 'receipts/call-0028-request.json'
    response_path = run / 'receipts/call-0028-response.json'
    db = run / 'baseline/h05/data/memory.sqlite3'
    request = json.loads(request_path.read_text())
    returned = json.loads(response_path.read_text())
    payload = json.loads(request['prompt'].split('\n', 1)[1])
    snapshot = {k: payload[k] for k in ('project', 'session_id', 'job_kind', 'reconsideration', 'historical_context')}
    snapshot.update(job_id=JOB, revision=payload['current_state']['revision'],
                    state=payload['current_state'], sources=[
                        {k: v for k, v in event.items() if k != 'spans'} |
                        {'body': ''.join(span['text'] for span in event['spans'])}
                        for event in payload['events']])
    assert ep._span_catalog(snapshot)[0] == payload['events']
    # The original durable snapshot is deliberately erased on quarantine. The
    # request carries bounded state, not the original pre-selection state.
    jsonschema.validate(returned['output'], request['output_schema'])
    pins = {str(p): digest(p) for p in (request_path, response_path,
            Path(ep.__file__), args.runtime_root / 'codex_mem/project_memory.py')}
    results = []
    with tempfile.TemporaryDirectory(prefix='r21-h05-diagnosis-') as scratch:
        source = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        assert json.loads(source.execute('SELECT input_json FROM pm_jobs WHERE id=?', (JOB,)).fetchone()[0]) == {}
        for item in snapshot['sources']:
            body = source.execute('SELECT body FROM entries WHERE id=?', (item['id'],)).fetchone()[0]
            assert body[item['body_offset']:item['body_offset'] + len(item['body'])] == item['body']
        prior_id = returned['output']['episodes'][0]['continuation_of']
        prior = json.loads(source.execute('SELECT data_json FROM pm_objects WHERE id=?', (prior_id,)).fetchone()[0])
        disclosed = next(item for item in snapshot['state']['episodes'] if item['id'] == prior_id)
        for key in ('id', 'goal', 'summary', 'evidence', 'logical_episode_id', 'episode_version'):
            assert prior[key] == disclosed[key], key
        target = sqlite3.connect(Path(scratch) / 'memory.sqlite3')
        source.backup(target)
        target.close()
        source.close()
        with Store(scratch) as store:
            memory = ProjectMemory(store)

            def validate(name, native, context=snapshot):
                before = store._connection.total_changes
                stage = 'receipt'
                try:
                    output, *_ = ep._validate_runner_receipt(dict(returned, output=native))
                    for function in (ep._resolve_object_references, ep._resolve_evidence_refs, ep._resolve_quotes):
                        stage = function.__name__
                        output = function(output, context)
                    stage = '_omit_unsupported_optional_fields'
                    output, _ = ep._omit_unsupported_optional_fields(output)
                    stage = 'episode_processor._validate_output'
                    ep._validate_output(output, context)
                    stage = 'ProjectMemory._validate'
                    objects, relations, reconciliation = memory._validate(context, output, conservative_transitions=True)
                    result = {'probe': name, 'status': 'accepted', 'objects': len(objects),
                              'omitted_continuities': reconciliation.get('omitted_continuities_count')}
                except (ValueError, TypeError, KeyError) as error:
                    result = {'probe': name, 'status': 'rejected', 'stage': stage,
                              'exception': type(error).__name__, 'predicate': str(error)}
                assert store._connection.total_changes == before, 'validation mutated scratch database'
                results.append(result)
                return result

            result = validate('saved_exact_output', returned['output'])
            assert result['predicate'] == 'one continuation per logical episode is allowed in each job'
            if args.red:
                print(json.dumps({'result': result, 'native_schema': 'accepted', 'spans_exact': True, 'pins': pins}, indent=2))
                return 1
            # Remove one variable at a time; these are offline counterfactuals,
            # never replacement historical grades or committed model output.
            for index in (0, 1):
                trial = copy.deepcopy(returned['output'])
                trial['episodes'].pop(index)
                assert validate(f'remove_episode_{index}', trial)['status'] == 'accepted'
            trial = copy.deepcopy(returned['output'])
            trial['claims'] = []
            assert validate('remove_all_claims', trial)['status'] == 'rejected'
            trial = copy.deepcopy(returned['output'])
            for episode in trial['episodes']:
                for field in ('goal', 'rationale', 'outcome', 'open_work'):
                    episode[field] = ''
                    episode['evidence'][field] = []
            trial['claims'] = []
            assert validate('remove_optional_fields_and_claims', trial)['status'] == 'rejected'
            # Minimize summary/evidence to the same one exact original ref.
            ref = payload['events'][0]['spans'][0]['ref']
            for episode in trial['episodes']:
                episode['summary'] = 'A source describes a requested update.'
                episode['evidence']['summary'] = [ref]
            minimized = copy.deepcopy(trial)
            assert validate('minimal_two_continuations', trial)['status'] == 'rejected'
            one_source = dict(snapshot, sources=snapshot['sources'][:1])
            assert validate('minimal_one_source_two_continuations', trial, one_source)['status'] == 'rejected'
            for index in (0, 1):
                standalone = copy.deepcopy(trial)
                standalone['episodes'][index]['continuation_of'] = None
                standalone['episodes'][index]['evidence']['continuation_of'] = []
                assert validate(f'minimal_one_standalone_{index}', standalone)['status'] == 'accepted'
            # Invalid ref control proves actual ref validation runs first.
            bad = copy.deepcopy(trial)
            bad['episodes'][0]['evidence']['summary'] = ['not_a_disclosed_ref']
            assert validate('invalid_ref_control', bad)['predicate'] == 'unknown evidence ref'
            print(json.dumps({'native_schema': 'accepted', 'spans_exact': True,
                'snapshot_limit': 'request reconstruction; original quarantined input_json erased; not a full live-job replay',
                'original_source_bodies_and_relevant_prior_fields_match_disclosed_request': True,
                'no_validation_writes': True, 'results': results,
                'minimal_native_output': minimized, 'pins': pins}, indent=2))
    assert all(digest(Path(path)) == value for path, value in pins.items())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
