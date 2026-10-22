#!/usr/bin/env python3
"""Prepare public judge controls; dispatch only with explicit --live and trial budget."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from xml.sax.saxutils import escape, quoteattr

REPO = Path(__file__).resolve().parents[3]
DEFAULT_HELPER = REPO / 'scripts/project_memory_consolidation_experiment.py'
DEFAULT_CONTROLS = Path(__file__).with_name('judge-contract-controls.json')


def load_helper(path):
    sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location('public_judge_helper', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def control_payload(helper, control, namespace="00"):
    """Expected human labels and control names never enter the model payload."""
    originals, visible, blocks, original_events = {}, [], [], []
    for index, source in enumerate(control['sources']):
        text, field, role = (source[k] for k in ('text', 'field', 'role'))
        sid = f'source-{namespace}-{index+1:02d}'
        originals[sid] = {'source_role': role, 'fields': {field: text}}
        original_events.append({'source_id': sid, 'source_role': role, 'source_field': field, 'text': text})
        if source['visible']:
            entry = {'source_id': sid, 'source_field': field, 'source_role': role,
                     'start': 0, 'end': len(text), 'quote': text}
            visible.append(entry)
            attrs = ' '.join(k + '=' + quoteattr(str(entry[k])) for k in
                             ('source_id', 'source_field', 'source_role', 'start', 'end'))
            blocks.append('<source ' + attrs + '><quote_excerpt>' + escape(text) + '</quote_excerpt></source>')
    facts = [{'text': text, 'support': []} for text in control['required_facts']]
    case = {'id': 'answer-' + namespace, 'question': control['question'], 'retrieved_context_id': 'q',
            'gold': {'required_facts': facts, 'forbidden_inferences': control['forbidden_inferences']},
            'answer': {'id': 'answer-' + namespace, 'answer': control['answer'], 'uncertainties': [],
                       'citations': [{k: v for k, v in span.items()} for span in visible]}}
    return {'original_history': [{'events': original_events}],
            'shared_reader_visible_memory': ['<project-memory>' + ''.join(blocks) + '</project-memory>'],
            'cases': [case], 'judge_source_catalog': helper.judge_source_catalog(originals, visible)}


def calibrate(helper, run, protocol, controls_path, *, live=False):
    controls = helper.read(controls_path)
    if controls.get('fictional') is not True or len(controls['controls']) != 6:
        raise ValueError('calibration requires the six declared public synthetic controls')
    if helper.judge_prompt_version(protocol) != helper.WITNESS_JUDGE_PROMPT_VERSION:
        raise ValueError('calibration requires the optional witness judge contract')
    if (protocol['model'], protocol['reasoning_effort']) != ('gpt-6-luna', 'medium'):
        raise ValueError('calibration must use the same gpt-6-luna medium profile')
    prepared = [({'id': control['id'], 'expected': {'answer-00': control['expected']}},
                 control_payload(helper, control)) for control in controls['controls']]
    grouped = {'original_history': [], 'shared_reader_visible_memory': [], 'cases': [], 'judge_source_catalog': []}
    expected = {}
    for index, control in enumerate(controls['controls']):
        packet = control_payload(helper, control, namespace=f'{index+1:02d}')
        expected[packet['cases'][0]['id']] = control['expected']
        for name in grouped:
            grouped[name].extend(packet[name])
    for index, source in enumerate(grouped['judge_source_catalog']):
        source['ref'] = f'J{index+1:04d}'
    prepared.append(({'id': 'grouped-six', 'expected': expected}, grouped))
    plan = {'status': 'prepared_no_native_calls', 'control_count': len(controls['controls']),
            'packet_count': len(prepared), 'planned_native_calls': len(prepared),
            'controls_sha256': hashlib.sha256(controls_path.read_bytes()).hexdigest(),
            'request_sha256': [helper.digest(helper.judge_request(payload, protocol)) for _, payload in prepared]}
    if not live:
        return plan
    declaration = protocol.get('judge_calibration', {})
    if (declaration.get('controls_sha256') != plan['controls_sha256']
            or type(declaration.get('max_calls')) is not int or not len(prepared) <= declaration['max_calls'] <= 8
            or type(declaration.get('max_observed_tokens')) is not int
            or not 1 <= declaration['max_observed_tokens'] <= 200000):
        raise ValueError('protocol must pin public controls and declare calibration limits at most 8 calls/200000 tokens')
    run = run.resolve()
    if not str(run).startswith(('/private/tmp/', '/tmp/')):
        raise ValueError('calibration requires the isolated original trial root')
    metadata_path = run / 'run.json'
    metadata_bytes = metadata_path.read_bytes()  # Never initialize or revise the original clock.
    metadata = json.loads(metadata_bytes)
    if (metadata['budget_run'] != str(run) or metadata['protocol_sha256'] != helper.digest(protocol)
            or (run / 'freeze.json').exists()):
        raise ValueError('calibration requires the matching original budget root before freeze')
    with (run / 'judge-calibration.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            return dispatch_controls(helper, run, protocol, declaration, prepared, plan, metadata_path, metadata_bytes)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def dispatch_controls(helper, run, protocol, declaration, prepared, plan, metadata_path, metadata_bytes):
    """Serialize calibration while Capture owns shared native reservations."""
    results = []
    for control, payload in prepared:
        history = 'public-judge-calibration-' + control['id']
        rows = helper.ledger_calls(run / 'calls.jsonl')
        previous = [row for row in rows if row.get('variant') == 'calibration']
        if any(row.get('history_id') == history for row in previous):
            raise ValueError('public control already attempted; no automatic retry or regrade')
        usage = helper.usage(previous)
        if (not usage['token_accounting_complete'] or usage['attempted_calls'] >= declaration['max_calls']
                or usage['tokens']['total_tokens'] + protocol['limits']['per_call_token_reservation'] > declaration['max_observed_tokens']):
            raise RuntimeError('public calibration budget exhausted or unknown; no native dispatch')
        output = run / 'judge-calibration' / (control['id'] + '.json')
        if output.exists() or output.with_suffix('.rejected.json').exists():
            raise ValueError('public calibration receipt already exists; no overwrite')
        def before_dispatch():
            if metadata_path.read_bytes() != metadata_bytes or (run / 'freeze.json').exists():
                raise ValueError('public calibration requires unchanged original metadata before freeze at each dispatch')
        capture = helper.Capture(run, protocol, 'judge', 'calibration', history, dispatch_guard=before_dispatch)
        response = capture(helper.judge_request(payload, protocol))
        try:
            grades = helper.decode_judge_output(response.get('output'), payload['cases'], protocol,
                                                 payload['judge_source_catalog'])
        except ValueError as exc:
            helper.write(output.with_suffix('.rejected.json'), {'status': 'rejected', 'reason': str(exc),
                'questions_accepted': 0, 'retry_policy': 'stop_no_automatic_retry', 'attempt': capture.identity,
                'response_sha256': helper.digest(response), 'metrics': response.get('metrics')})
            raise
        actual = {grade['id']: {'fact_recall': [fact['recalled'] for fact in grade['fact_recall']],
                               'critical_errors': grade['critical_errors']} for grade in grades}
        matched = set(actual) == set(control['expected']) and all(
            actual[key]['fact_recall'] == expected['fact_recall']
            and set(actual[key]['critical_errors']) == set(expected['critical_errors'])
            for key, expected in control['expected'].items())
        result = {'control_id': control['id'], 'status': 'matched' if matched else 'human_label_mismatch',
                  'expected': control['expected'], 'actual': actual, 'grades': grades,
                  'attempt': capture.identity, 'metrics': response.get('metrics'),
                  'response_sha256': helper.digest(response), 'request_sha256': helper.digest(helper.judge_request(payload, protocol))}
        helper.write(output, result)
        results.append(result)
        if metadata_path.read_bytes() != metadata_bytes:
            raise RuntimeError('original trial metadata changed during public calibration')
        if not matched:
            raise RuntimeError('public judge calibration disagrees with human control; stop without retry or rescore')
    summary = {**plan, 'status': 'completed', 'results': results}
    helper.write(run / 'judge-calibration/summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--helper', type=Path, default=DEFAULT_HELPER)
    parser.add_argument('--controls', type=Path, default=DEFAULT_CONTROLS)
    parser.add_argument('--live', action='store_true', help='Use the explicitly declared existing trial clock and native ledger.')
    args = parser.parse_args()
    helper = load_helper(args.helper)
    result = calibrate(helper, args.run, helper.read(args.protocol), args.controls, live=args.live)
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
