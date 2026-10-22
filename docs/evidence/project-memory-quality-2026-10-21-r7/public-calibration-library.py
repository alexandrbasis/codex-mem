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


def prepare_packets(helper, controls):
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
    return prepared


def expectation_matches(actual, expected):
    alternatives = expected.get('critical_error_alternative_sets', [expected['critical_errors']])
    return actual['fact_recall'] == expected['fact_recall'] and any(
        set(actual['critical_errors']) == set(option) for option in alternatives)


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_oracle(controls):
    version = controls.get('oracle_version')
    reviewed = [control for control in controls['controls'] if control['id'] == 'real-causal-conflation']
    if version is not None and (version != 'public-critical-overlap-v3' or len(reviewed) != 1
            or reviewed[0]['expected'].get('critical_error_alternative_sets') != [['false_merge'], ['project_mixing'], ['false_merge', 'project_mixing']]):
        raise ValueError('public oracle v3 requires exactly the reviewed overlapping critical sets')
    for control in controls['controls']:
        alternatives = control['expected'].get('critical_error_alternative_sets')
        if alternatives is not None and (
                control['id'] != 'real-causal-conflation'
                or alternatives != [['false_merge'], ['project_mixing'], ['false_merge', 'project_mixing']]
                or control['expected']['critical_errors'] != ['false_merge']
                or control['expected']['fact_recall'] != [True, False]):
            raise ValueError('public oracle alternatives are restricted to the reviewed causal taxonomy case')


def build_continuation_ack(helper, run, original_protocol_path, original_controls_path, original_run_path):
    """Read-only proposed manifest. The parent must explicitly pin and authorize it."""
    original_protocol_text = original_protocol_path.read_text()
    original_controls_text = original_controls_path.read_text()
    original_run_text = original_run_path.read_text()
    protocol, controls = json.loads(original_protocol_text), json.loads(original_controls_text)
    rows = helper.ledger_calls(run / 'calls.jsonl')
    prior = []
    for control, payload in prepare_packets(helper, controls)[:5]:
        history = 'public-judge-calibration-' + control['id']
        starts = [row for row in rows if row.get('status') == 'started' and row.get('history_id') == history]
        if len(starts) != 1:
            raise ValueError('continuation acknowledgment requires exactly one original attempt per prior packet')
        call_id = starts[0]['call_id']
        request_path = run / 'receipts' / (call_id + '-request.json')
        response_path = run / 'receipts' / (call_id + '-response.json')
        result_path = run / 'judge-calibration' / (control['id'] + '.json')
        request = helper.read(request_path)
        if helper.serialized_request(request) != helper.serialized_request(helper.judge_request(payload, protocol)):
            raise ValueError('prior calibration request differs from the original public packet')
        prior.append({'control_id': control['id'], 'call_id': call_id,
            'original_status': helper.read(result_path)['status'],
            'request_file_sha256': file_sha256(request_path), 'response_file_sha256': file_sha256(response_path),
            'result_file_sha256': file_sha256(result_path),
            'serialized_request_sha256': hashlib.sha256(helper.serialized_request(request)).hexdigest()})
    return {'schema_version': 1, 'oracle_version': 'public-critical-taxonomy-v2',
        'original_protocol_json': original_protocol_text, 'original_controls_json': original_controls_text,
        'original_run_json': original_run_text, 'original_protocol_sha256': helper.digest(protocol),
        'original_controls_sha256': hashlib.sha256(original_controls_text.encode()).hexdigest(),
        'original_run_sha256': hashlib.sha256(original_run_text.encode()).hexdigest(),
        'prior_ledger_sha256': file_sha256(run / 'calls.jsonl'),
        'native_profile': {'model': protocol['model'], 'reasoning_effort': protocol['reasoning_effort']},
        'prior_packets': prior, 'remaining_packet_ids': ['original-only-visibility', 'grouped-six'],
        'taxonomy_acknowledgment': {'control_id': 'real-causal-conflation',
            'original_status': 'human_label_mismatch', 'facts': [True, False],
            'original_expected_critical_errors': ['false_merge'], 'observed_critical_errors': ['project_mixing'],
            'reason': 'Independent Spec review accepts either subtype for this affirmative neighboring-project causal conflation. Original mismatch remains immutable; no regrade.'}}


def validate_continuation(helper, run, protocol, controls, ack_path):
    """Validate immutable receipts, explicit revision, and exactly two unattempted packets."""
    ack = helper.read(ack_path)
    declaration = protocol['judge_calibration'].get('continuation', {})
    if (ack.get('schema_version') != 1 or ack.get('oracle_version') != 'public-critical-taxonomy-v2'
            or declaration.get('ack_manifest_sha256') != file_sha256(ack_path)
            or declaration.get('oracle_version') != ack['oracle_version']
            or declaration.get('previous_protocol_sha256') != ack.get('original_protocol_sha256')):
        raise ValueError('continuation requires a pinned acknowledgment and explicit new pre-freeze protocol revision')
    old_protocol = json.loads(ack['original_protocol_json'])
    old_controls = json.loads(ack['original_controls_json'])
    old_metadata = json.loads(ack['original_run_json'])
    if (helper.digest(old_protocol) != ack['original_protocol_sha256']
            or old_protocol['judge_calibration']['controls_sha256'] != ack['original_controls_sha256']
            or hashlib.sha256(ack['original_controls_json'].encode()).hexdigest() != ack['original_controls_sha256']
            or hashlib.sha256(ack['original_run_json'].encode()).hexdigest() != ack['original_run_sha256']):
        raise ValueError('continuation original proof hashes do not match')
    for name in ('limits', 'model', 'reasoning_effort', 'pass_criteria', 'critical_errors',
                 'judge_evidence_contract', 'reader_citation_contract'):
        if protocol.get(name) != old_protocol.get(name):
            raise ValueError('continuation may not change runtime profile, budget or grading criteria')
    if any(protocol['judge_calibration'][name] != old_protocol['judge_calibration'][name]
           for name in ('max_calls', 'max_observed_tokens')):
        raise ValueError('continuation may not reset or raise the calibration subbudget')
    normalized = json.loads(json.dumps(controls))
    normalized.pop('oracle_version', None); normalized.pop('oracle_revision_note', None)
    for control in normalized['controls']:
        control['expected'].pop('critical_error_alternative_sets', None)
    if normalized != old_controls or controls.get('oracle_version') != ack['oracle_version']:
        raise ValueError('continuation may change only the reviewed public critical subtype oracle')
    current_metadata = helper.read(run / 'run.json')
    if (old_metadata.get('budget_run') != str(run)
            or old_metadata.get('protocol_sha256') != ack['original_protocol_sha256']
            or current_metadata != {**old_metadata, 'protocol_sha256': helper.digest(protocol)}):
        raise ValueError('continuation must preserve every original root metadata field and clock')
    audit = helper.calls(run / 'protocol-revisions.jsonl')
    revisions = [row for row in audit if row.get('status') == 'prepared'
        and row.get('previous_sha256') == ack['original_protocol_sha256']
        and row.get('new_sha256') == helper.digest(protocol)
        and any(change.get('path') == 'run.json' and change.get('previous_run_json') == ack['original_run_json']
                for change in row.get('changes', []))]
    if not any(any(row.get('status') == 'applied' and row.get('revision_id') == revision['revision_id']
                   and row.get('new_sha256') == helper.digest(protocol) for row in audit) for revision in revisions):
        raise ValueError('continuation requires the explicit applied pre-freeze revision audit')
    rows = helper.ledger_calls(run / 'calls.jsonl')
    if file_sha256(run / 'calls.jsonl') != ack.get('prior_ledger_sha256'):
        raise ValueError('continuation ledger changed or includes an extra attempt')
    usage = helper.usage(rows)
    if (usage['attempted_calls'] != 5 or usage['finished_calls'] != 5
            or usage['failed_calls'] or usage['unresolved_calls'] or not usage['token_accounting_complete']):
        raise ValueError('continuation requires exactly five fully observed original calibration attempts')
    if ack.get('native_profile') != {'model': protocol['model'], 'reasoning_effort': protocol['reasoning_effort']}:
        raise ValueError('continuation native profile differs')
    expected_remaining = ['original-only-visibility', 'grouped-six']
    if ack.get('remaining_packet_ids') != expected_remaining:
        raise ValueError('continuation must dispatch only the two originally unattempted packets')
    old_prepared = prepare_packets(helper, old_controls)
    new_prepared = prepare_packets(helper, controls)
    if len(ack.get('prior_packets', [])) != 5:
        raise ValueError('continuation must bind all five prior packets')
    preserved = []
    for index, receipt in enumerate(ack['prior_packets']):
        control, payload = old_prepared[index]
        expected_status = 'matched' if index < 4 else 'human_label_mismatch'
        call_id = receipt.get('call_id', '')
        if not isinstance(call_id, str) or not helper.re.fullmatch(r'call-[0-9]{4}', call_id):
            raise ValueError('continuation has an invalid original call binding')
        request_path = run / 'receipts' / (call_id + '-request.json')
        response_path = run / 'receipts' / (call_id + '-response.json')
        result_path = run / 'judge-calibration' / (control['id'] + '.json')
        if (receipt.get('control_id') != control['id'] or receipt.get('original_status') != expected_status
                or any(file_sha256(path) != receipt.get(key) for path, key in (
                    (request_path, 'request_file_sha256'), (response_path, 'response_file_sha256'),
                    (result_path, 'result_file_sha256')))):
            raise ValueError('continuation prior request, response or result hash/status changed')
        request, response, result = helper.read(request_path), helper.read(response_path), helper.read(result_path)
        serialized = helper.serialized_request(request)
        history = 'public-judge-calibration-' + control['id']
        starts = [row for row in rows if row.get('status') == 'started' and row.get('history_id') == history]
        if (len(starts) != 1 or starts[0]['call_id'] != call_id or starts[0].get('variant') != 'calibration'
                or starts[0].get('protocol_sha256') != ack['original_protocol_sha256']
                or starts[0].get('helper_sha256') != file_sha256(Path(helper.__file__))
                or starts[0].get('prompt_version') != helper.judge_prompt_version(protocol)
                or starts[0].get('phase') != 'judge' or starts[0].get('run_id') != 'primary'
                or starts[0].get('repeat') != 0
                or starts[0].get('request_sha256') != helper.digest(request)
                or starts[0].get('model') != protocol['model']
                or starts[0].get('reasoning_effort') != protocol['reasoning_effort']
                or hashlib.sha256(serialized).hexdigest() != receipt.get('serialized_request_sha256')
                or serialized != helper.serialized_request(helper.judge_request(payload, old_protocol))
                or serialized != helper.serialized_request(helper.judge_request(new_prepared[index][1], protocol))
                or result.get('status') != expected_status or result.get('expected') != control['expected']
                or result.get('response_sha256') != helper.digest(response)):
            raise ValueError('continuation prior packet/profile/request/output binding differs')
        if index < 4:
            if result.get('actual') != result['expected']:
                raise ValueError('continuation prior matched packet is inconsistent')
        else:
            acknowledgment = ack.get('taxonomy_acknowledgment', {})
            if (acknowledgment.get('control_id') != control['id']
                    or acknowledgment.get('original_status') != expected_status
                    or acknowledgment.get('facts') != [True, False]
                    or acknowledgment.get('original_expected_critical_errors') != ['false_merge']
                    or acknowledgment.get('observed_critical_errors') != ['project_mixing']
                    or result.get('actual') != {'answer-00': {'fact_recall': [True, False], 'critical_errors': ['project_mixing']}}):
                raise ValueError('continuation acknowledgment may only address the reviewed taxonomy mismatch')
        preserved.append({'control_id': control['id'], 'original_status': expected_status,
            'continuation_disposition': 'prior_matched' if index < 4 else 'taxonomy_mismatch_acknowledged',
            'result_file_sha256': receipt['result_file_sha256'], 'call_id': call_id,
            'request_file_sha256': receipt['request_file_sha256'], 'response_file_sha256': receipt['response_file_sha256']})
    for index in (5, 6):
        control, payload = new_prepared[index]
        if (helper.serialized_request(helper.judge_request(payload, protocol))
                != helper.serialized_request(helper.judge_request(old_prepared[index][1], old_protocol))):
            raise ValueError('continuation changed an unattempted model payload or schema')
        path = run / 'judge-calibration' / (control['id'] + '.json')
        if path.exists() or path.with_suffix('.rejected.json').exists():
            raise ValueError('continuation remaining packet already has a receipt')
    return preserved


def calibrate(helper, run, protocol, controls_path, *, live=False, continue_unattempted=False, ack_path=None):
    controls = helper.read(controls_path)
    if controls.get('fictional') is not True or len(controls['controls']) != 6:
        raise ValueError('calibration requires the six declared public synthetic controls')
    if helper.judge_prompt_version(protocol) != helper.WITNESS_JUDGE_PROMPT_VERSION:
        raise ValueError('calibration requires the optional witness judge contract')
    if (protocol['model'], protocol['reasoning_effort']) != ('gpt-6-luna', 'medium'):
        raise ValueError('calibration must use the same gpt-6-luna medium profile')
    validate_oracle(controls)
    if continue_unattempted != (ack_path is not None):
        raise ValueError('explicit continuation requires both --continue-unattempted and --ack-manifest')
    prepared = prepare_packets(helper, controls)
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
            prior = []
            if continue_unattempted:
                prior = validate_continuation(helper, run, protocol, controls, ack_path)
                prepared = prepared[5:]
                plan['continuation_ack_sha256'] = file_sha256(ack_path)
            return dispatch_controls(helper, run, protocol, declaration, prepared, plan, metadata_path, metadata_bytes, prior)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def dispatch_controls(helper, run, protocol, declaration, prepared, plan, metadata_path, metadata_bytes, prior=None):
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
            if prior:
                if helper.usage(helper.ledger_calls(run / 'calls.jsonl'))['attempted_calls'] != 5 + len(results):
                    raise ValueError('continuation encountered an extra native attempt before dispatch')
                for saved in prior:
                    paths = [(run / 'receipts' / (saved['call_id'] + '-request.json'), 'request_file_sha256'),
                             (run / 'receipts' / (saved['call_id'] + '-response.json'), 'response_file_sha256'),
                             (run / 'judge-calibration' / (saved['control_id'] + '.json'), 'result_file_sha256')]
                    if any(file_sha256(path) != saved[key] for path, key in paths):
                        raise ValueError('continuation prior immutable receipt changed before dispatch')
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
            expectation_matches(actual[key], expected)
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
    summary = {**plan, 'status': 'completed_with_taxonomy_acknowledgment' if prior else 'completed',
               'prior_results': prior or [], 'results': results}
    if prior:
        summary['counts'] = {'prior_matched': 4, 'acknowledged_taxonomy_mismatch': 1, 'new_matched': len(results)}
    helper.write(run / 'judge-calibration/summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--helper', type=Path, default=DEFAULT_HELPER)
    parser.add_argument('--controls', type=Path, default=DEFAULT_CONTROLS)
    parser.add_argument('--continue-unattempted', action='store_true', help='Explicit pre-freeze continuation of exactly two never-attempted packets.')
    parser.add_argument('--ack-manifest', type=Path, help='Pinned acknowledgment of five immutable prior public packets.')
    parser.add_argument('--live', action='store_true', help='Use the explicitly declared existing trial clock and native ledger.')
    args = parser.parse_args()
    helper = load_helper(args.helper)
    result = calibrate(helper, args.run, helper.read(args.protocol), args.controls, live=args.live,
                       continue_unattempted=args.continue_unattempted, ack_path=args.ack_manifest)
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
