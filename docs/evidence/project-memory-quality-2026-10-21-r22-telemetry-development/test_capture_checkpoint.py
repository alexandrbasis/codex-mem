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
ROOT = Path(__file__).resolve().parents[3]
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
