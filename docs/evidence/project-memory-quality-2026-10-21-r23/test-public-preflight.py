"""Offline checks for the first-dispatch guard and missing preregistration pins."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicPreflightTests(unittest.TestCase):
    def test_exact_tool_field_mirror_requires_same_source_quote_and_visible_bounds(self):
        probe = load('test_r13_public_mirror_probe', HERE / 'prepare-reader-probe.py')
        originals = {'source-a': {'fields': {'body': 'Result: failed.',
                                            'tool_response.output': 'Result: failed.'}}}
        support = {'source_id': 'source-a', 'start': 0, 'end': 15, 'quote': 'Result: failed.'}
        catalog = [{'source_id': 'source-a', 'source_field': 'tool_response.output',
                    'start': 0, 'end': 15, 'ref': 'C1'}]
        with self.assertRaisesRegex(ValueError, 'outside its selected visible'):
            probe.visible_support(catalog, support, originals)
        mirror = {**support, 'reader_visible_equivalent': {
            'source_field': 'tool_response.output', 'start': 0, 'end': 15}}
        receipt = probe.visible_support(catalog, mirror, originals)
        self.assertEqual(receipt['source_field'], 'tool_response.output')
        self.assertEqual(receipt['gold_source_field'], 'body')
        self.assertEqual(receipt['quote'], support['quote'])
        for invalid in ({**catalog[0], 'source_id': 'source-b'},
                        {**catalog[0], 'end': 14}):
            with self.assertRaisesRegex(ValueError, 'outside its selected visible'):
                probe.visible_support([invalid], mirror, originals)
        changed = {'source-a': {'fields': {**originals['source-a']['fields'],
                                          'tool_response.output': 'Result: passed.'}}}
        with self.assertRaisesRegex(ValueError, 'exact same-source field mirror'):
            probe.visible_support(catalog, mirror, changed)
        with self.assertRaisesRegex(ValueError, 'original source span'):
            probe.visible_support(catalog, {**mirror, 'quote': 'Result: passed.'}, originals)

    def test_declared_new_calibration_namespace_preserves_prior_attempts(self):
        library = load('test_r13_namespaced_calibration', HERE / 'public-calibration-library.py')
        histories = []

        class Capture:
            def __init__(self, run, protocol, phase, variant, history, *, dispatch_guard):
                histories.append(history)
                self.guard = dispatch_guard

            def __call__(self, request):
                self.guard()
                raise ValueError('stopped_before_native')

        for prior_history, expected in (
                ('public-judge-calibration-grouped-six', 'stopped_before_native'),
                ('public-judge-calibration-reader-sol-v1-grouped-six', 'already attempted')):
            with self.subTest(prior_history=prior_history), tempfile.TemporaryDirectory() as directory:
                run = Path(directory)
                metadata = run / 'run.json'
                metadata.write_bytes(b'{}')
                original = run / 'judge-calibration' / 'grouped-six.json'
                original.parent.mkdir()
                original.write_bytes(b'preserved original')
                helper = types.SimpleNamespace(
                    identifier=lambda value: value,
                    ledger_calls=lambda path: [{'variant': 'calibration', 'history_id': prior_history}],
                    usage=lambda rows: {'token_accounting_complete': True, 'attempted_calls': len(rows),
                                        'tokens': {'total_tokens': 0}},
                    judge_request=lambda payload, protocol: {}, Capture=Capture)
                with self.assertRaisesRegex(ValueError, expected):
                    library.dispatch_controls(helper, run,
                        {'limits': {'per_call_token_reservation': 100}},
                        {'max_calls': 1, 'max_observed_tokens': 1000, 'attempt_id': 'reader-sol-v1'},
                        [({'id': 'grouped-six'}, {})], {}, metadata, b'{}')
                self.assertEqual(original.read_bytes(), b'preserved original')
        self.assertEqual(histories, ['public-judge-calibration-reader-sol-v1-grouped-six'])

    def test_complete_public_fixture_contract_loads_without_injected_globals(self):
        contract = load('test_r13_public_fixture_contract', HERE / 'public-fixture-contract.py')
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'fixture.json'
            fixture.write_text('{"fictional": false}')
            with self.assertRaisesRegex(ValueError, 'explicitly fictional'):
                contract.fixture(fixture)

    def test_first_calibration_dispatch_runs_full_guard_before_native(self):
        library = load('test_r13_calibration', HERE / 'public-calibration-library.py')
        dispatched = []

        class Capture:
            def __init__(self, *args, dispatch_guard):
                self.guard = dispatch_guard

            def __call__(self, request):
                self.guard()
                dispatched.append(request)
                raise AssertionError('native dispatch must be unreachable')

        helper = types.SimpleNamespace(
            ledger_calls=lambda path: [],
            usage=lambda rows: {'token_accounting_complete': True, 'attempted_calls': 0,
                                'tokens': {'total_tokens': 0}},
            judge_request=lambda payload, protocol: {}, Capture=Capture)

        def changed():
            raise ValueError('source_pin_changed')

        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            metadata = run / 'run.json'
            metadata.write_bytes(b'{}')
            with self.assertRaisesRegex(ValueError, 'source_pin_changed'):
                library.dispatch_controls(helper, run,
                    {'limits': {'per_call_token_reservation': 100}},
                    {'max_calls': 1, 'max_observed_tokens': 1000},
                    [({'id': 'grouped-six'}, {})], {}, metadata, b'{}',
                    extra_dispatch_guard=changed)
        self.assertEqual(dispatched, [])

    def test_fresh_namespaced_root_creates_parents_but_never_retries_leaf(self):
        runner = load('test_r13_fresh_namespaced_runner', HERE / 'run-public-preflight.py')
        helper = runner.load('test_r13_fresh_helper', HERE.parents[2] / 'scripts/project_memory_consolidation_experiment.py')
        protocol = json.loads((HERE.parent / 'project-memory-quality-2026-10-21-r21/protocol.json').read_text())
        protocol['pipeline_receipt_contract']='complete-shared-pipeline-v1'
        paths=list((runner.ROOT/'codex_mem').glob('*.py'))+[runner.ROOT/'scripts/project_memory_consolidation_experiment.py']
        paths += [HERE/name for name in ('run-public-preflight.py','prepare-reader-probe.py','public-reader-regressions.json','public-calibration-library.py','judge-contract-controls.json')]
        protocol['public_preflight']['file_sha256']={str(path.relative_to(runner.ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        original_load = runner.load

        def stopped_load(name, path):
            module = original_load(name, path)
            if path.name == 'public-calibration-library.py':
                module.dispatch_controls = mock.Mock(side_effect=RuntimeError('reached_first_dispatch'))
            return module

        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            path = run / 'protocol.json'
            path.write_text(json.dumps(protocol))
            (run / 'run.json').write_text(json.dumps({'budget_run': str(run),
                'started_at': 1, 'protocol_sha256': helper.digest(protocol)}))
            with mock.patch.object(runner, 'load', side_effect=stopped_load):
                with self.assertRaisesRegex(RuntimeError, 'reached_first_dispatch'):
                    runner.execute(run, path)
                leaf = run / 'public-preflight' / protocol['public_preflight']['attempt_id']
                self.assertTrue(leaf.is_dir())
                preserved = (leaf / 'stopped.json').read_bytes()
                with self.assertRaises(FileExistsError):
                    runner.execute(run, path)
                self.assertEqual((leaf / 'stopped.json').read_bytes(), preserved)
                self.assertFalse((run / 'calls.jsonl').exists())

    def test_missing_pins_reject_before_any_public_directory_or_call(self):
        runner = load('test_r13_preflight_runner', HERE / 'run-public-preflight.py')
        protocol = {'reader_fidelity_contract':'quantified-source-fidelity-v1','public_preflight': {'file_sha256': {},'fresh_reader_calls':8,'fresh_public_reader_judge_packets':1,'fresh_known_control_judge_packets':1,'planned_native_calls':10,'reader_fidelity_contract':'quantified-source-fidelity-v1'}}
        digest = hashlib.sha256(json.dumps(protocol, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            protocol_path = run / 'protocol.json'
            protocol_path.write_text(json.dumps(protocol))
            (run / 'run.json').write_text(json.dumps({'protocol_sha256': digest,
                'budget_run': str(run), 'started_at': 1}))
            with self.assertRaisesRegex(ValueError, 'requires complete product'):
                runner.execute(run, protocol_path)
            self.assertFalse((run / 'public-preflight').exists())
            self.assertFalse((run / 'calls.jsonl').exists())

    def test_missing_new_ten_call_fidelity_plan_rejects_before_directory_or_dispatch(self):
        runner=load('test_r23_missing_plan',HERE/'run-public-preflight.py')
        protocol={'public_preflight':{'file_sha256':{}}}
        digest=hashlib.sha256(json.dumps(protocol,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory);path=run/'protocol.json';path.write_text(json.dumps(protocol))
            (run/'run.json').write_text(json.dumps({'protocol_sha256':digest,'budget_run':str(run),'started_at':1}))
            with self.assertRaisesRegex(ValueError,'eight isolated fidelity readers'):runner.execute(run,path)
            self.assertFalse((run/'public-preflight').exists())
            self.assertFalse((run/'calls.jsonl').exists())

    def test_misbound_budget_root_rejects_before_any_call(self):
        runner = load('test_r13_misbound_runner', HERE / 'run-public-preflight.py')
        protocol = {'public_preflight': {'file_sha256': {}}}
        digest = hashlib.sha256(json.dumps(protocol, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            protocol_path = run / 'protocol.json'
            protocol_path.write_text(json.dumps(protocol))
            (run / 'run.json').write_text(json.dumps({'protocol_sha256': digest,
                'budget_run': str(run / 'other-root'), 'started_at': 1}))
            with self.assertRaisesRegex(ValueError, 'canonical original budget root'):
                runner.execute(run, protocol_path)
            self.assertFalse((run / 'public-preflight').exists())
            self.assertFalse((run / 'calls.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
