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
    """Expected labels and control names never enter the model payload."""
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
    memory = '<project-memory>' + ''.join(blocks) + '</project-memory>'
    case['reader_visible_memory'] = [memory]
    return {'original_history': [{'events': original_events}],
            'cases': [case],
            'case_source_catalogs': {case['id']: helper.judge_source_catalog(originals, visible)}}


def prepare_packets(helper, controls):
    prepared = [({'id': control['id'], 'expected': {'answer-00': control['expected']}},
                 control_payload(helper, control)) for control in controls['controls']]
    grouped = {'original_history': [], 'cases': [], 'case_source_catalogs': {}}
    expected = {}
    for index, control in enumerate(controls['controls']):
        packet = control_payload(helper, control, namespace=f'{index+1:02d}')
        expected[packet['cases'][0]['id']] = control['expected']
        grouped['original_history'].extend(packet['original_history'])
        grouped['cases'].extend(packet['cases'])
        grouped['case_source_catalogs'].update(packet['case_source_catalogs'])
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
                                                 payload['case_source_catalogs'])
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
        result = {'control_id': control['id'], 'status': 'matched' if matched else 'known_oracle_mismatch',
                  'expected': control['expected'], 'actual': actual, 'grades': grades,
                  'attempt': capture.identity, 'metrics': response.get('metrics'),
                  'response_sha256': helper.digest(response), 'request_sha256': helper.digest(helper.judge_request(payload, protocol))}
        helper.write(output, result)
        results.append(result)
        if metadata_path.read_bytes() != metadata_bytes:
            raise RuntimeError('original trial metadata changed during public calibration')
        if not matched:
            raise RuntimeError('public judge calibration disagrees with known control; stop without retry or rescore')
    summary = {**plan, 'status': 'completed', 'results': results}
    helper.write(run / 'judge-calibration/summary.json', summary)
    return summary
