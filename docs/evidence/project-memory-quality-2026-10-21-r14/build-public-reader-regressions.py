"""Prepare six disclosed R13 regressions from new read-only source views."""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SELECTED = ('hold_1002/q3', 'hold_1003/q4', 'hold_1006/q2', 'hold_1010/q6', 'hold_1016/q1', 'hold_1007/q3')


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(replay, run, output):
    spec = importlib.util.spec_from_file_location('r14_corpus_probe', HERE / 'prepare-reader-probe.py')
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    helper = probe.load_helper()
    evidence = ROOT / 'docs/evidence/project-memory-quality-2026-10-21-r13/holdout'
    histories = {history['id']: history for history in read(evidence / 'fixture.json')['histories']}
    gold = read(evidence / 'gold.json')['questions']
    contexts = {item['case']: item['new_context'] for item in read(replay / 'new-contexts.json')}
    result = read(replay / 'result.json')
    if result['product_hashes'] != helper.code_hash(ROOT):
        raise ValueError('disclosed replay belongs to different product bytes')
    cases = []
    for index, key in enumerate(SELECTED, 1):
        history_id, _ = key.split('/')
        history = histories[history_id]
        generation_path = run / 'candidate' / history_id / 'generation.json'
        generation = read(generation_path)
        source_map = generation['source_map']
        originals = helper.original_source_fields(history, source_map)
        catalog = helper.reader_citation_catalog([contexts[key]], originals)
        mapped_gold = copy.deepcopy(gold[key])
        for fact in mapped_gold['required_facts']:
            for support in fact['support']:
                support['source_id'] = source_map[support['event_id']]
                try:
                    probe.visible_support(catalog, support, originals)
                except ValueError:
                    # An atomic tool capture can expose its canonical output
                    # field instead of a duplicate body excerpt. Keep the
                    # original gold offsets and record only an exact mirror.
                    fields = originals[support['source_id']]['fields']
                    for span in catalog:
                        field = span['source_field']
                        if span['source_id'] != support['source_id'] or field == 'body':
                            continue
                        start = fields[field].find(support['quote'], span['start'], span['end'])
                        if start >= 0:
                            support['reader_visible_equivalent'] = {'source_field': field,
                                'start': start, 'end': start + len(support['quote'])}
                            probe.visible_support(catalog, support, originals)
                            break
        original = copy.deepcopy(history['sessions'])
        for session in original:
            for event in session['events']:
                event['source_id'] = source_map.get(event['id'])
        cases.append({'id': f'public-reader-{index:02d}', 'focus': mapped_gold['category'],
            'provenance': {'disclosed_case': key, 'generation_sha256': sha(generation_path),
                'replay_contexts_sha256': sha(replay / 'new-contexts.json'),
                'fixture_sha256': sha(evidence / 'fixture.json'),
                'gold_sha256': sha(evidence / 'gold.json'),
                'fresh_views_not_old_answers_or_grades': True},
            'context': contexts[key], 'original_history': original,
            'original_source_fields': originals,
            'gold': mapped_gold, 'oracle': {
                'required_facts_recalled': [True] * len(mapped_gold['required_facts']),
                'critical_errors_allowed': [], 'citations_supported': True,
                'claim': 'Known disclosed regression only. All required facts and scope must survive.',
                'frozen_before_new_native_calls': True}})
    protocol = read(HERE / 'protocol.json')
    corpus = {'schema_version': 1, 'fictional': True, 'status': 'prepared_no_native_calls',
        'corpus': 'Six disclosed R13 histories with new source views; no unreleased R14 inputs.',
        'selection': 'Preregistered generic scope change, late approval, original reason, assistant-report allocation, '
                     'surviving constraint and citation-only cases. Purposive regression only.',
        'proof_policy': 'All required original support spans must be visible before the first dispatch. '
                        'No old answer or grade is retried, replaced, or rescored.',
        'planned_native_calls': {'fresh_reader_probes': 6, 'fresh_probe_judge_batch': 1,
                                'fresh_known_judge_control': 1, 'total': 8,
                                'all_inside_new_trial_limits': True},
        'reader_contract': {name: protocol[name] for name in ('reader_citation_contract',
            'reader_context_isolation', 'reader_evidence_contract', 'model', 'reasoning_effort',
            'context_chars', 'reader_profile')},
        'expected_probe_policy': 'All six fresh answers fully supported with supported citations and '
                                 'zero critical errors. Stop on any failure without retry or regrade.',
        'cases': cases}
    prepared = probe.prepare(helper, protocol, corpus)
    if output.exists():
        raise FileExistsError('public regression corpus already exists; no automatic replacement')
    helper.write(output, corpus)
    print(json.dumps({'status': 'prepared_no_native_calls', 'cases': len(cases),
        'required_facts': sum(len(case['gold']['required_facts']) for case in cases),
        'sha256': sha(output), 'request_sha256': prepared['request_sha256']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replay', type=Path, required=True)
    parser.add_argument('--run', type=Path,
                        default=Path('/private/tmp/codex-mem-quality-20261021-r13-experiment'))
    parser.add_argument('--output', type=Path, default=HERE / 'public-reader-regressions.json')
    args = parser.parse_args()
    build(args.replay.resolve(), args.run.resolve(), args.output.resolve())
