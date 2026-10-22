#!/usr/bin/env python3
"""Run the declared nine public controls once under the original trial budget."""
import argparse
import hashlib
import importlib.util
import math
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def load(name, path):
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def producer_control(helper, run, protocol, destination, unchanged, namespace):
    """Exercise one native v12 owner relation without opening sealed inputs."""
    from codex_mem import episode_processor
    probe = load('r12_public_producer_builder', HERE / 'prepare-producer-probe.py')
    unchanged()
    plan = probe.prepare(destination / 'producer-store')
    capture = helper.Capture(run, protocol, 'generation', 'public-producer-probe',
        'public-producer-' + (namespace or 'v1'), dispatch_guard=unchanged)
    native = {}

    def once(request):
        if 'request' in native:
            raise ValueError('public producer permits exactly one native dispatch')
        native['request'] = request
        native['response'] = capture(request)
        return native['response']

    result = episode_processor.process_project_memory(
        plan['project'], plan['data'], runner=once, timeout=protocol['limits']['call_seconds'])
    helper.write(destination / 'producer-processor-receipt.json', result)
    if set(native) != {'request', 'response'}:
        raise ValueError('public producer did not complete its one native call')
    receipt = probe.verify(plan, result, native['response'], native_request=native['request'])
    helper.write(destination / 'producer-summary.json', receipt)
    unchanged()
    return receipt


def execute(run, protocol_path):
    helper = load('r12_preflight_helper', ROOT / 'scripts/project_memory_consolidation_experiment.py')
    probe = load('r12_public_reader_builder', HERE / 'prepare-reader-probe.py')
    calibration = load('r12_public_judge_controls', HERE / 'public-calibration-library.py')
    protocol = helper.read(protocol_path)
    root_metadata = run / 'run.json'
    original_metadata = root_metadata.read_bytes()
    state = helper.read(root_metadata)
    if (not isinstance(state.get('budget_run'), str)
            or Path(state['budget_run']) != run or not run.is_absolute()):
        raise ValueError('public preflight requires its canonical original budget root')
    if (type(state.get('started_at')) not in (int, float)
            or not math.isfinite(state['started_at']) or state['started_at'] < 0):
        raise ValueError('public preflight requires a valid original run clock')
    if (state['protocol_sha256'] != helper.digest(protocol)
            or (run / 'freeze.json').exists()):
        raise ValueError('public preflight requires the current original clock before freeze')
    declaration = protocol['public_preflight']
    required = {str(path.relative_to(ROOT)) for path in (ROOT / 'codex_mem').glob('*.py')}
    required.update(str(path.relative_to(ROOT)) for path in (
        ROOT / 'scripts/project_memory_consolidation_experiment.py',
        HERE / 'run-public-preflight.py', HERE / 'prepare-reader-probe.py',
        HERE / 'public-reader-regressions.json', HERE / 'public-calibration-library.py',
        HERE / 'judge-contract-controls.json', HERE / 'prepare-producer-probe.py'))
    if not required <= set(declaration['file_sha256']):
        raise ValueError('public preflight requires complete product/helper/control/runner pins')
    for name, pin in declaration['file_sha256'].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != pin:
            raise ValueError('public preflight bytes differ from preregistered hashes')
    namespace = declaration.get('attempt_id')
    if namespace is not None:
        helper.identifier(namespace)
    destination = run / 'public-preflight' / namespace if namespace else run / 'public-preflight'
    destination.mkdir(parents=True, exist_ok=False)
    corpus = helper.read(HERE / 'public-reader-regressions.json')
    prepared = probe.prepare(helper, protocol, corpus)
    helper.write(destination / 'prepared-request-hashes.json', {
        key: value for key, value in prepared.items()
        if key not in ('reader_requests', 'evaluator_references_not_reader_input')})

    def unchanged():
        if (root_metadata.read_bytes() != original_metadata or (run / 'freeze.json').exists()
                or helper.digest(helper.read(protocol_path)) != helper.digest(protocol)):
            raise ValueError('original public-preflight protocol/clock changed or freeze started')
        for name, pin in declaration['file_sha256'].items():
            if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != pin:
                raise ValueError('public preflight source changed before native dispatch')

    try:
        controls = helper.read(HERE / 'judge-contract-controls.json')
        calibration.validate_oracle(controls)
        grouped = calibration.prepare_packets(helper, controls)[-1:]
        calibration.dispatch_controls(helper, run, protocol, protocol['judge_calibration'],
            grouped, {'status': 'planned', 'control_count': 6, 'packet_count': 1,
                      'planned_native_calls': 1, 'interpretation': 'Fresh known-control regression.'},
            root_metadata, original_metadata, extra_dispatch_guard=unchanged)
        producer = producer_control(helper, run, protocol, destination, unchanged, namespace)
        outputs = []
        for index, item in enumerate(prepared['reader_requests']):
            capture = helper.Capture(run, protocol, 'reader', 'public-reader-probe',
                'public-reader-' + (namespace + '-' if namespace else '') + item['case_id'],
                dispatch_guard=unchanged)
            capture.identity['question_id'] = item['case_id']
            response = capture(item['request'])
            reference = prepared['evaluator_references_not_reader_input'][index]
            resolved = helper.resolve_reader_output(response.get('output'),
                [reference['context']], reference['catalog'])
            helper.write(destination / f'reader-{index+1:02d}.json', {
                'status': 'validated', 'case_id': item['case_id'],
                'attempt': capture.identity, 'response': response, 'resolved': resolved})
            outputs.append(response.get('output'))
        payload = probe.judge_payload(helper, protocol, corpus, outputs)
        request = helper.judge_request(payload, protocol)
        capture = helper.Capture(run, protocol, 'judge', 'public-reader-probe',
            'public-reader-' + (namespace + '-' if namespace else '') + 'six',
            dispatch_guard=unchanged)
        response = capture(request)
        grades = helper.decode_judge_output(response.get('output'), payload['cases'], protocol,
                                            payload['case_source_catalogs'])
        passed = len(grades) == 6 and all(
            grade['full_supported_answer'] and grade['citations_supported']
            and not grade['critical_errors'] for grade in grades)
        summary = {'status': 'passed' if passed else 'failed', 'fresh_reader_questions': 6,
                   'fresh_judge_packets': 2, 'fresh_producer_calls': 1,
                   'producer': producer, 'grades': grades,
                   'interpretation': 'Disclosed regression only. No independent holdout score.',
                   'native_usage': helper.usage(helper.ledger_calls(run / 'calls.jsonl'))}
        helper.write(destination / 'summary.json', summary)
        if not passed:
            raise RuntimeError('public reader regression gate failed; stop without retry or regrade')
        unchanged()
        print('public preflight passed: one producer, six isolated readers and two judge packets')
    except Exception as exc:
        helper.write(destination / 'stopped.json', {'status': 'stopped_no_automatic_retry',
                     'error_type': type(exc).__name__, 'reason': str(exc),
                     'native_usage': helper.usage(helper.ledger_calls(run / 'calls.jsonl'))})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, default=HERE / 'protocol.json')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('native dispatch requires explicit --live')
    run = args.run.resolve()
    if not str(run).startswith('/private/tmp/'):
        parser.error('trial directory must resolve under /private/tmp/')
    execute(run, args.protocol.resolve())


if __name__ == '__main__':
    main()
