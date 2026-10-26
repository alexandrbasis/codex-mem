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
                # Keep the normal startup budget so usage can arrive before the
                # timeout fixture withholds completion for thirty seconds.
                result = NativeProcessorRunner(codex=str(self.executable)).execute(self.request())
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

    def test_native_dispatch_guard_denies_initial_and_correction_turns_without_usage_invention(self):
        from codex_mem.execution import ExecutionProfile
        from codex_mem.pi_runtime import PiProcessorRunner
        from codex_mem.processor import ProcessorFailure
        for runtime in ('codex', 'pi'):
            for denied_turn in (1, 2):
                with self.subTest(runtime=runtime, denied_turn=denied_turn):
                    executable = self.root / ('guard-' + runtime + '-' + str(denied_turn))
                    fixture = Path(__file__).parent / 'fixtures' / ('execution_' + runtime + '.py')
                    executable.write_text('#!' + sys.executable + '\n' + fixture.read_text())
                    executable.chmod(0o700)
                    executable.with_suffix('.json').write_text(json.dumps(dict(observation=True,
                        output={'result': {'notes': [], 'disposition': 'skipped', 'session_summary': None}})))
                    guarded = []
                    def guard():
                        guarded.append(len(guarded) + 1)
                        if len(guarded) == denied_turn:
                            raise ProcessorFailure('invalid_response', reason_code='summary_support_stale')
                    options = dict(before_dispatch=guard, candidate_review=lambda *_: 'Correct this candidate.')
                    if runtime == 'codex':
                        runner = NativeProcessorRunner(codex=str(executable), **options)
                        profile = ExecutionProfile('codex', 'openai', 'gpt-6-luna', 'medium', 8)
                    else:
                        runner = PiProcessorRunner(pi=str(executable), **options)
                        profile = ExecutionProfile('pi', 'fixture', 'fixture', 'low', 8)
                    prompt = '<untrusted_observations>\n[{"id":"s1","body":"Synthetic original report."}]\n</untrusted_observations>'
                    result = runner.execute(self.request(profile=profile, deadline=time.monotonic() + 8,
                        prompt=prompt))
                    self.assertEqual('summary_support_stale', result.failure.reason_code, result)
                    trace = [json.loads(line) for line in executable.with_suffix('.trace').read_text().splitlines()]
                    submitted = [row for row in trace if row.get('method') == 'turn/start' or row.get('type') == 'prompt']
                    self.assertEqual(denied_turn - 1, len(submitted))
                    self.assertEqual(denied_turn, len(guarded))
                    if denied_turn == 1:
                        self.assertIsNone(result.usage['tokens'])
                    else:
                        self.assertEqual(12, result.usage['tokens']['total_tokens'])

    def test_expired_attempt_never_starts_a_worker(self):
        result = NativeProcessorRunner(codex='/missing/executable').execute(self.request(deadline=time.monotonic() - 1))
        self.assertEqual('timeout', result.failure.reason)
        self.assertIsNone(result.usage['tokens'])

    def test_catalog_requires_exact_model_field_and_follows_bounded_pages(self):
        from codex_mem.execution import ExecutionProfile
        selected = ExecutionProfile('codex', 'openai', 'fixture-exact', 'low', 5)
        candidate = {'id': 'picker-id', 'model': 'fixture-exact',
                     'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}
        self.scenario(model_pages={'first': {'data': [], 'nextCursor': 'page2'},
                                   'page2': {'data': [candidate]}})
        result = NativeProcessorRunner(codex=str(self.executable)).execute(self.request(profile=selected))
        self.assertIsNone(result.failure)
        self.assertEqual('fixture-exact', result.identity.model)
        for invalid in ({**candidate, 'id': 'fixture-exact', 'model': 'substitute'},
                        {'id': 'fixture-exact', 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}):
            with self.subTest(candidate=invalid):
                self.scenario(models=[invalid])
                result = NativeProcessorRunner(codex=str(self.executable)).execute(self.request(profile=selected))
                self.assertEqual('configuration', result.failure.reason)
                self.assertIsNone(result.identity)

    def test_catalog_failures_share_attempt_deadline_and_page_bound(self):
        from types import SimpleNamespace
        from unittest import mock
        from codex_mem import processor
        for scenario, expected in (({'model_pages': {'first': {'data': 'malformed'}}}, 'protocol'),
                                   ({'catalog_mode': 'disconnect'}, 'protocol'),
                                   ({'catalog_mode': 'timeout'}, 'timeout'),
                                   ({'model_pages': {'first': {'data': [], 'nextCursor': 'loop'},
                                                     'loop': {'data': [], 'nextCursor': 'loop'}}}, 'configuration')):
            with self.subTest(scenario=scenario):
                self.scenario(**scenario)
                trace = self.executable.with_suffix('.trace')
                trace.unlink(missing_ok=True)
                watchdog_seconds = 30
                watchdog_expired = [False]
                catalog_expiry = []

                def records():
                    if not trace.exists():
                        return []
                    text = trace.read_text()
                    lines = text.splitlines()
                    if text and not text.endswith('\n'):
                        lines = lines[:-1]
                    return [json.loads(line) for line in lines]

                started = time.monotonic()
                request = self.request(deadline=started + 2)
                original_deadline = request.deadline
                clock = [started]

                def monotonic():
                    rows = records()
                    methods = [row.get('method') for row in rows]
                    catalogs = [row for row in rows if row.get('method') == 'model/list']
                    # Charge logical preflight/page time to the original two-second
                    # attempt, without making fixture startup speed its subject.
                    elapsed = .25 * sum(method in methods for method in ('initialize', 'account/read'))
                    clock[0] = max(clock[0], started + elapsed + .05 * len(catalogs))
                    if scenario.get('catalog_mode') == 'timeout' and catalogs and not catalog_expiry:
                        # Expire only after the real child received model/list.
                        catalog_expiry.append(clock[0])
                        clock[0] = original_deadline + .001
                    # A separate real watchdog fails explicitly, never as a catalog
                    # result. The runner's finally closes its owned process/pipes.
                    if time.monotonic() - started >= watchdog_seconds and not watchdog_expired[0]:
                        watchdog_expired[0] = True
                        # Raise once regardless of any client deadline regression.
                        # Later clock reads stay finite so finally can record and reap.
                        raise AssertionError('Fixture exceeded the real catalog-test watchdog.')
                    return clock[0]

                with mock.patch.object(processor, 'time', SimpleNamespace(monotonic=monotonic)):
                    result = NativeProcessorRunner(codex=str(self.executable)).execute(request)
                rows = records()
                methods = [row.get('method') for row in rows]
                catalogs = [row for row in rows if row.get('method') == 'model/list']
                self.assertFalse(watchdog_expired[0], {'scenario': scenario, 'methods': methods})
                self.assertEqual(['initialize', 'initialized', 'account/read'], methods[:3])
                self.assertEqual(expected, result.failure.reason, {'result': result, 'methods': methods})
                self.assertIsNone(result.output)
                self.assertIsNone(result.identity)
                self.assertIsNone(result.usage['tokens'])
                self.assertEqual(original_deadline, request.deadline)
                self.assertNotIn('thread/start', methods)
                self.assertNotIn('turn/start', methods)
                if expected == 'configuration':
                    self.assertEqual('exact_model_unavailable', result.failure.reason_code)
                    self.assertEqual(processor.MAX_MODEL_PAGES, len(catalogs))
                    self.assertEqual([None] + ['loop'] * (processor.MAX_MODEL_PAGES - 1),
                                     [row['params'].get('cursor') for row in catalogs])
                else:
                    self.assertEqual(1, len(catalogs), methods)
                if expected == 'timeout':
                    self.assertEqual(1, len(catalog_expiry))
                    self.assertGreater(catalog_expiry[0], started)
                    self.assertLess(catalog_expiry[0], original_deadline)
                    self.assertGreater(clock[0], original_deadline)
                else:
                    self.assertEqual([], catalog_expiry)
                    self.assertLess(clock[0], original_deadline)
                # Thirty seconds is a test hang detector, not a latency promise.
                # Allow the existing runner's five-second termination sequence.
                self.assertLess(time.monotonic() - started, watchdog_seconds + 5)
