"""Offline checks for the first-dispatch guard and missing preregistration pins."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicPreflightTests(unittest.TestCase):
    def test_complete_public_fixture_contract_loads_without_injected_globals(self):
        contract = load('test_r11_public_fixture_contract', HERE / 'public-fixture-contract.py')
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'fixture.json'
            fixture.write_text('{"fictional": false}')
            with self.assertRaisesRegex(ValueError, 'explicitly fictional'):
                contract.fixture(fixture)

    def test_first_calibration_dispatch_runs_full_guard_before_native(self):
        library = load('test_r11_calibration', HERE / 'public-calibration-library.py')
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

    def test_missing_pins_reject_before_any_public_directory_or_call(self):
        runner = load('test_r11_preflight_runner', HERE / 'run-public-preflight.py')
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
        runner = load('test_r11_misbound_runner', HERE / 'run-public-preflight.py')
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
