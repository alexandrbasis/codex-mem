"""Prospective Capture contract. All native work is replaced at its boundary."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SCRIPT = ROOT / 'scripts/project_memory_consolidation_experiment.py'
SPEC = importlib.util.spec_from_file_location('checkpoint_experiment', SCRIPT)
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)
from codex_mem.processor import ProcessorFailure


def metrics(status, total=None):
    tokens = None if total is None else dict(input_tokens=total-10,
        cached_input_tokens=0, cache_write_input_tokens=0, output_tokens=10,
        reasoning_output_tokens=0, total_tokens=total)
    return {'duration_ms': 2, 'usage': {'status': status,
        'source': 'app_server_thread_total', 'updates': int(total is not None),
        'tokens': tokens}}


class CaptureCheckpointContract(unittest.TestCase):
    def capture_with(self, runner):
        temporary = self.enterContext(tempfile.TemporaryDirectory(prefix='r22-checkpoint-control-'))
        run = Path(temporary)
        protocol = {'model': 'gpt-6-luna', 'reasoning_effort': 'medium',
            'limits': {'call_seconds': 10, 'wall_seconds': 1000, 'max_calls': 10,
                'per_call_token_reservation': 100, 'max_observed_tokens': 10000}}
        experiment.write(run / 'run.json', {'started_at': experiment.time.time(), 'budget_run': str(run)})
        adapter = types.SimpleNamespace(NativeProcessorRunner=runner,
            MODEL='gpt-6-luna', REASONING_EFFORT='medium')
        self.enterContext(patch.object(experiment, 'native_phase_adapter', return_value=adapter))
        return run, experiment.Capture(run, protocol, 'generation', 'baseline', 'public-case')

    def test_checkpoint_is_observable_but_unknown_final_still_stops_dispatch(self):
        # Real Capture and ledger accounting. The fake replaces only native execution.
        observed_callbacks = []
        snapshots = []
        class FakeNative:
            def __init__(self, *, timeout, usage_checkpoint=None):
                observed_callbacks.append(usage_checkpoint)
                self.checkpoint = usage_checkpoint

            def __call__(self, request):
                snapshot = {'worker_thread_id': 'public-thread',
                    'worker_turn_id': 'public-turn', 'metrics': metrics('partial', 25),
                    'raw_error': 'SENSITIVE_SENTINEL', 'prompt': 'SENSITIVE_SENTINEL'}
                snapshots.append(snapshot)
                if self.checkpoint:
                    self.checkpoint(snapshot)
                if request['prompt'] == 'Public successful request.':
                    return {'output': {}, 'metrics': metrics('reported', 100)}
                raise ProcessorFailure('runner_failure', reason_code='native_server_error',
                    metrics=metrics('unavailable'))

        adapter = types.SimpleNamespace(NativeProcessorRunner=FakeNative,
            MODEL='gpt-6-luna', REASONING_EFFORT='medium')
        protocol = {'model': 'gpt-6-luna', 'reasoning_effort': 'medium',
            'limits': {'call_seconds': 10, 'wall_seconds': 1000, 'max_calls': 10,
                'per_call_token_reservation': 100, 'max_observed_tokens': 10000}}
        with tempfile.TemporaryDirectory(prefix='r22-checkpoint-contract-') as temporary:
            run = Path(temporary)
            experiment.write(run / 'run.json', {'started_at': experiment.time.time(),
                'budget_run': str(run)})
            capture = experiment.Capture(run, protocol, 'generation', 'baseline', 'public-case')
            with patch.object(experiment, 'native_phase_adapter', return_value=adapter):
                capture({'prompt': 'Public successful request.'})
                first = experiment.usage(experiment.ledger_calls(run / 'calls.jsonl'))
                self.assertEqual(first['attempted_calls'], 1)
                self.assertEqual(first['tokens']['total_tokens'], 100)
                self.assertTrue(first['token_accounting_complete'])
                with self.assertRaises(ProcessorFailure):
                    capture({'prompt': 'Public failed request.'})
                rows = experiment.ledger_calls(run / 'calls.jsonl')
                self.assertEqual([row['status'] for row in rows],
                    ['started', 'completed', 'started', 'failed'])
                totals = experiment.usage(rows)
                self.assertEqual(totals['attempted_calls'], 2)
                self.assertEqual(totals['finished_calls'], 2)
                self.assertEqual(totals['reported_tokens_lower_bound']['total_tokens'], 100)
                self.assertIsNone(totals['tokens']['total_tokens'])
                self.assertFalse(totals['token_accounting_complete'])
                with self.assertRaisesRegex(RuntimeError, 'experiment_usage_unknown_stop_for_review'):
                    experiment.Capture(run, protocol, 'generation', 'baseline', 'public-case')(
                        {'prompt': 'Public request must not dispatch.'})
                self.assertEqual(len(observed_callbacks), 2)
                self.assertEqual(len(experiment.ledger_calls(run / 'calls.jsonl')), 4)
            # Forwarding must happen at the real call-site, not in the fake's result.
            self.assertTrue(all(callable(value) for value in observed_callbacks),
                'Capture discarded the native usage checkpoint callback')
            for call_id in ('call-0001', 'call-0002'):
                saved = experiment.read(run / 'receipts' / (call_id + '-checkpoint.json'))
                self.assertEqual(saved['schema_version'], 'native-usage-checkpoint-v1')
                self.assertEqual(saved['call_id'], call_id)
                self.assertEqual(saved['worker_thread_id'], 'public-thread')
                self.assertEqual(saved['worker_turn_id'], 'public-turn')
                self.assertEqual(saved['metrics'], metrics('partial', 25))
                self.assertNotIn('SENSITIVE_SENTINEL', json.dumps(saved))
            self.assertFalse((run / 'receipts/call-0002-response.json').exists())
            self.assertFalse((run / 'native-request-cache.jsonl').exists())

    def test_latest_reported_checkpoint_cannot_replace_unknown_terminal_usage(self):
        import os
        import stat
        run = None
        synced = []
        real_fsync = os.fsync
        def observe_fsync(descriptor):
            synced.append('directory' if stat.S_ISDIR(os.fstat(descriptor).st_mode) else 'file')
            real_fsync(descriptor)
        class FakeNative:
            def __init__(self, *, timeout, usage_checkpoint):
                self.checkpoint = usage_checkpoint
            def __call__(self, request):
                with patch.object(experiment.os, 'fsync', side_effect=observe_fsync):
                    for state, total in (('partial', 25), ('reported', 150)):
                        self.checkpoint({'worker_thread_id': 'public-thread',
                            'worker_turn_id': 'public-turn', 'metrics': metrics(state, total)})
                # The callback persisted before the terminal exception.
                saved = experiment.read(run / 'receipts/call-0001-checkpoint.json')
                self_test.assertEqual(saved['metrics']['usage']['tokens']['total_tokens'], 150)
                self_test.assertEqual(len(experiment.ledger_calls(run / 'calls.jsonl')), 1)
                raise ProcessorFailure('runner_failure', metrics=metrics('unavailable'))
        self_test = self
        run, capture = self.capture_with(FakeNative)
        with self.assertRaises(ProcessorFailure):
            capture({'prompt': 'Public request.'})
        self.assertEqual(len(list((run / 'receipts').glob('*-checkpoint.json'))), 1)
        self.assertEqual(synced, ['file', 'directory', 'file', 'directory'])
        self.assertLess((run / 'receipts/call-0001-checkpoint.json').stat().st_size, 4096)
        self.assertFalse(list((run / 'receipts').glob('*.tmp')))
        totals = experiment.usage(experiment.ledger_calls(run / 'calls.jsonl'))
        self.assertEqual(totals['attempted_calls'], 1)
        self.assertEqual(totals['finished_calls'], 1)
        self.assertEqual(totals['calls_with_reported_tokens'], 0)
        self.assertIsNone(totals['tokens']['total_tokens'])
        self.assertFalse((run / 'native-request-cache.jsonl').exists())

    def test_malformed_checkpoint_cannot_replace_last_valid_snapshot(self):
        import copy
        good = {'worker_thread_id': 'public-thread', 'worker_turn_id': None,
            'metrics': metrics('unavailable')}
        malformed = [None, {}, {**good, 'worker_thread_id': 'bad/id'},
            {**good, 'worker_turn_id': 'x'*257}]
        for field, value in (('duration_ms', True), ('duration_ms', 2**63),
                ('usage', {'status': 'unavailable'})):
            item = copy.deepcopy(good)
            item['metrics'][field] = value
            malformed.append(item)
        for field, value in (('status', 'SENSITIVE_SENTINEL'), ('source', 'SENSITIVE_SENTINEL'),
                ('updates', True), ('updates', -1), ('updates', 2**63),
                ('tokens', {'total_tokens': 0})):
            item = copy.deepcopy(good)
            item['metrics']['usage'][field] = value
            malformed.append(item)
        for bad_tokens in ({}, {'total_tokens': 0}, {**metrics('partial', 25)['usage']['tokens'],
                'total_tokens': 2**63}, {**metrics('partial', 25)['usage']['tokens'],
                'input_tokens': True}):
            item = copy.deepcopy(good)
            item['metrics'] = metrics('partial', 25)
            item['metrics']['usage']['tokens'] = bad_tokens
            malformed.append(item)
        run = None
        class FakeNative:
            def __init__(self, *, timeout, usage_checkpoint):
                self.checkpoint = usage_checkpoint
            def __call__(self, request):
                for item in malformed:
                    self.checkpoint(item)
                self_test.assertFalse((run / 'receipts/call-0001-checkpoint.json').exists())
                self.checkpoint(good)
                for item in malformed:
                    self.checkpoint(item)
                return {'output': {}, 'metrics': metrics('reported', 100)}
        self_test = self
        run, capture = self.capture_with(FakeNative)
        capture({'prompt': 'Public request.'})
        saved = experiment.read(run / 'receipts/call-0001-checkpoint.json')
        self.assertEqual(saved['metrics'], metrics('unavailable'))
        self.assertIsNone(saved['metrics']['usage']['tokens'])
        self.assertNotIn('SENSITIVE_SENTINEL', json.dumps(saved))
        self.assertEqual(experiment.usage(experiment.ledger_calls(run / 'calls.jsonl'))['tokens']['total_tokens'], 100)

    def test_checkpoint_storage_failure_preserves_success_and_original_failure(self):
        class FakeNative:
            def __init__(self, *, timeout, usage_checkpoint):
                self.checkpoint = usage_checkpoint
            def __call__(self, request):
                with patch.object(experiment.os, 'fsync', side_effect=OSError('SENSITIVE_SENTINEL')):
                    self.checkpoint({'worker_thread_id': 'public-thread',
                        'worker_turn_id': 'public-turn', 'metrics': metrics('partial', 25)})
                if request['prompt'] == 'Public success.':
                    return {'output': {}, 'metrics': metrics('reported', 100)}
                raise failure
        failure = ProcessorFailure('runner_failure', reason_code='native_server_error',
            metrics=metrics('unavailable'))
        run, capture = self.capture_with(FakeNative)
        self.assertEqual(capture({'prompt': 'Public success.'})['output'], {})
        with self.assertRaises(ProcessorFailure) as caught:
            capture({'prompt': 'Public failure.'})
        self.assertIs(caught.exception, failure)
        self.assertFalse(list((run / 'receipts').glob('*-checkpoint.json')))
        self.assertFalse(list((run / 'receipts').glob('*.tmp')))
        rows = experiment.ledger_calls(run / 'calls.jsonl')
        self.assertEqual([row['status'] for row in rows], ['started', 'completed', 'started', 'failed'])
        self.assertNotIn('SENSITIVE_SENTINEL', json.dumps(rows))


if __name__ == '__main__':
    unittest.main(verbosity=2)
