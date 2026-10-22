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
    def test_declared_new_calibration_namespace_preserves_prior_attempts(self):
        library = load('test_r12_namespaced_calibration', HERE / 'public-calibration-library.py')
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
        contract = load('test_r12_public_fixture_contract', HERE / 'public-fixture-contract.py')
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'fixture.json'
            fixture.write_text('{"fictional": false}')
            with self.assertRaisesRegex(ValueError, 'explicitly fictional'):
                contract.fixture(fixture)

    def test_first_calibration_dispatch_runs_full_guard_before_native(self):
        library = load('test_r12_calibration', HERE / 'public-calibration-library.py')
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
        runner = load('test_r12_fresh_namespaced_runner', HERE / 'run-public-preflight.py')
        helper = runner.load('test_r12_fresh_helper', HERE.parents[2] / 'scripts/project_memory_consolidation_experiment.py')
        protocol = json.loads((HERE / 'protocol.json').read_text())
        for name in protocol['public_preflight']['file_sha256']:
            protocol['public_preflight']['file_sha256'][name] = hashlib.sha256((runner.ROOT / name).read_bytes()).hexdigest()
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

    def test_producer_allows_one_dispatch_and_binds_request_to_verifier(self):
        runner = load('test_r12_producer_once_runner', HERE / 'run-public-preflight.py')
        from codex_mem import episode_processor
        for attempts in (1, 2):
            dispatched, verified, writes, guards = [], [], [], []
            plan = {'project': 'fictional-project', 'data': 'fictional-data'}
            request, response = {'prompt': 'fictional'}, {'output': {'claims': []}}

            class Capture:
                def __init__(self, *args, dispatch_guard):
                    self.guard = dispatch_guard

                def __call__(self, item):
                    self.guard()
                    dispatched.append(item)
                    return response

            def process(project, data, *, runner, timeout):
                self.assertEqual((project, data, timeout), ('fictional-project', 'fictional-data', 180))
                for _ in range(attempts):
                    runner(request)
                return {'status': 'processed'}

            def verify(actual_plan, result, raw, *, native_request):
                self.assertIs(actual_plan, plan)
                self.assertIs(raw, response)
                self.assertIs(native_request, request)
                verified.append(result)
                return {'status': 'passed'}

            probe = types.SimpleNamespace(prepare=lambda path: plan, verify=verify)
            helper = types.SimpleNamespace(Capture=Capture, write=lambda path, value: writes.append(path.name))
            with tempfile.TemporaryDirectory() as directory, mock.patch.object(runner, 'load', return_value=probe), mock.patch.object(episode_processor, 'process_project_memory', side_effect=process):
                args = (helper, Path(directory), {'limits': {'call_seconds': 180}},
                        Path(directory), lambda: guards.append(True), 'public-v1')
                if attempts == 1:
                    self.assertEqual(runner.producer_control(*args), {'status': 'passed'})
                    self.assertEqual(len(verified), 1)
                    self.assertIn('producer-summary.json', writes)
                else:
                    with self.assertRaisesRegex(ValueError, 'exactly one native dispatch'):
                        runner.producer_control(*args)
                    self.assertEqual(verified, [])
            self.assertEqual(dispatched, [request])

    def test_missing_pins_reject_before_any_public_directory_or_call(self):
        runner = load('test_r12_preflight_runner', HERE / 'run-public-preflight.py')
        protocol = {'public_preflight': {'file_sha256': {}}}
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

    def test_misbound_budget_root_rejects_before_any_call(self):
        runner = load('test_r12_misbound_runner', HERE / 'run-public-preflight.py')
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
