"""Exercise the execution contract at the external process boundary."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from codex_mem.processor import NativeProcessorRunner


class ExecutionContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.executable = self.root / 'codex-fixture'
        fixture = Path(__file__).parent / 'fixtures' / 'execution_codex.py'
        self.executable.write_text('#!' + sys.executable + '\n' + fixture.read_text())
        self.executable.chmod(0o700)
        self.scenario()

    def scenario(self, **values):
        self.executable.with_suffix('.json').write_text(json.dumps(values))

    def request(self, **changes):
        from codex_mem.execution import ExecutionRequest, legacy_profile
        values = dict(prompt='Bounded input', output_schema={'type': 'object'},
                      job_type='observation', profile=legacy_profile(5),
                      deadline=time.monotonic() + 5, attempt_id='attempt-1')
        values.update(changes)
        return ExecutionRequest(**values)

    def test_completed_execution_has_verified_identity_and_usage(self):
        result = NativeProcessorRunner(codex=str(self.executable)).execute(self.request())
        self.assertIsNone(result.failure)
        self.assertEqual({'result': {'notes': [], 'disposition': 'skipped', 'session_summary': None}}, result.output)
        self.assertEqual(('codex', 'openai', 'gpt-6-luna', 'medium', 'fixture-worker', 'turn-1'),
                         (result.identity.runtime, result.identity.provider, result.identity.model,
                          result.identity.reasoning_effort, result.identity.execution_id, result.identity.turn_id))
        self.assertEqual(12, result.usage['tokens']['total_tokens'])
        self.assertEqual('reported', result.usage['status'])

    def test_failures_keep_measured_usage_without_a_partial_result(self):
        for mode, expected in (('invalid_json', 'invalid_output'), ('cancelled', 'cancelled'),
                               ('tool', 'forbidden_tools'), ('rerouted', 'identity_mismatch'),
                               ('disconnect', 'protocol'), ('timeout', 'timeout')):
            with self.subTest(mode=mode):
                self.scenario(mode=mode)
                result = NativeProcessorRunner(codex=str(self.executable)).execute(
                    self.request(deadline=time.monotonic() + (2 if mode == 'timeout' else 5)))
                self.assertIsNone(result.output)
                self.assertEqual(expected, result.failure.reason)
                self.assertEqual(12, result.usage['tokens']['total_tokens'])
        self.scenario(model='different-model')
        result = NativeProcessorRunner(codex=str(self.executable)).execute(self.request())
        self.assertEqual('identity_mismatch', result.failure.reason)
        self.assertIsNone(result.usage['tokens'])

    def test_one_correction_shares_deadline_and_cumulative_usage(self):
        reviewed = []
        def review(output, remaining):
            reviewed.append(remaining)
            return 'Correct only the supplied evidence.' if len(reviewed) == 1 else None
        result = NativeProcessorRunner(codex=str(self.executable), candidate_review=review).execute(self.request())
        self.assertIsNone(result.failure)
        self.assertEqual('turn-2', result.identity.turn_id)
        self.assertEqual(24, result.usage['tokens']['total_tokens'])
        self.assertEqual(2, len(reviewed))
        self.assertLess(reviewed[1], reviewed[0])
        result = NativeProcessorRunner(codex=str(self.executable), candidate_review=lambda *_: 'Again').execute(self.request())
        self.assertEqual('invalid_output', result.failure.reason)
        self.assertEqual(24, result.usage['tokens']['total_tokens'])

    def test_expired_attempt_never_starts_a_worker(self):
        result = NativeProcessorRunner(codex='/missing/executable').execute(self.request(deadline=time.monotonic() - 1))
        self.assertEqual('timeout', result.failure.reason)
        self.assertIsNone(result.usage['tokens'])
