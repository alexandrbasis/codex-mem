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

    def generation_fixture(self, runtime, scenario, **options):
        from codex_mem.execution import ExecutionProfile
        from codex_mem.pi_runtime import PiProcessorRunner
        executable = self.root / ('generation-' + runtime)
        fixture = Path(__file__).parent / 'fixtures' / ('execution_' + runtime + '.py')
        executable.write_text('#!' + sys.executable + '\n' + fixture.read_text())
        executable.chmod(0o700)
        executable.with_suffix('.json').write_text(json.dumps(scenario))
        executable.with_suffix('.trace').unlink(missing_ok=True)
        runner = (NativeProcessorRunner(codex=str(executable), **options) if runtime=='codex'
                  else PiProcessorRunner(pi=str(executable), **options))
        profile = ExecutionProfile(runtime, 'openai' if runtime=='codex' else 'fixture',
                                   'gpt-6-luna' if runtime=='codex' else 'fixture',
                                   'medium' if runtime=='codex' else 'low', 8)
        prompt = json.dumps({'events': [{'spans': [{'ref': 's1', 'text': 'Synthetic payload record.'}]}]})
        return runner, self.request(profile=profile, deadline=time.monotonic()+8, prompt=prompt), executable

    def submitted_generations(self, executable):
        return [row for row in (json.loads(line) for line in executable.with_suffix('.trace').read_text().splitlines())
                if row.get('method')=='turn/start' or row.get('type')=='prompt']

    def test_failure_feedback_only_receives_initial_generation_json_failure(self):
        from codex_mem.execution import ExecutionFailure
        for runtime in ('codex', 'pi'):
            for job_type in ('observation', 'summary', 'summary_support', 'project_memory'):
                with self.subTest(runtime=runtime, job_type=job_type):
                    feedback = []
                    def review(failure, remaining):
                        self.assertIsInstance(failure, ExecutionFailure)
                        self.assertEqual(('invalid_output','invalid_response','invalid_json'),
                                         (failure.reason, failure.code, failure.reason_code))
                        self.assertGreater(remaining, 0)
                        self.assertIsNotNone(failure.execution_id)
                        self.assertIsNotNone(failure.turn_id)
                        feedback.append(failure)
                        return 'Return a complete JSON replacement under the original schema.'
                    runner, request, executable = self.generation_fixture(runtime, dict(mode='invalid_json'),
                                                                         failure_review=review)
                    from dataclasses import replace
                    result = runner.execute(replace(request, job_type=job_type))
                    eligible = job_type in ('observation','summary')
                    self.assertEqual(1 if eligible else 0, len(feedback))
                    self.assertEqual(2 if eligible else 1, len(self.submitted_generations(executable)))
                    self.assertEqual('invalid_json', result.failure.reason_code)
                    self.assertEqual(24 if eligible else 12, result.usage['tokens']['total_tokens'])

    def test_ineligible_native_failures_never_receive_json_feedback(self):
        from codex_mem.processor import MAX_MODEL_OUTPUT_CHARS
        for runtime in ('codex', 'pi'):
            scenarios = [dict(mode=mode) for mode in ('cancelled','provider_error','tool','rerouted','disconnect')]
            scenarios.append(dict(mode='overflow') if runtime=='pi' else
                             dict(output={'oversized': 'x' * (MAX_MODEL_OUTPUT_CHARS+1)}))
            if runtime=='pi': scenarios.append(dict(mode='malformed'))
            for scenario in scenarios:
                with self.subTest(runtime=runtime, mode=scenario.get('mode','output_limit')):
                    feedback = []
                    def review(failure, remaining):
                        feedback.append(failure)
                        return 'This callback must never run.'
                    runner, request, executable = self.generation_fixture(runtime, scenario, failure_review=review)
                    result = runner.execute(request)
                    self.assertIsNotNone(result.failure)
                    mode = scenario.get('mode', 'output_limit')
                    expected = {'cancelled':'cancelled', 'provider_error':'runtime_failure',
                        'tool':'forbidden_tools', 'rerouted':'identity_mismatch', 'disconnect':'protocol',
                        'overflow':'protocol', 'malformed':'protocol', 'output_limit':'invalid_output'}[mode]
                    self.assertEqual(expected, result.failure.reason, result)
                    if runtime=='pi' and mode!='disconnect':
                        self.assertNotEqual('pi_disconnected', result.failure.reason_code)
                    self.assertNotEqual('invalid_json', result.failure.reason_code)
                    self.assertEqual([], feedback)
                    self.assertEqual(1, len(self.submitted_generations(executable)))
                    self.assertIsNone(result.output)

    def test_json_replacement_dispatch_guard_keeps_first_failure_and_usage(self):
        from codex_mem.processor import ProcessorFailure
        for runtime in ('codex','pi'):
            with self.subTest(runtime=runtime):
                guards = []
                def guard():
                    guards.append(1)
                    if len(guards)==2:
                        raise ProcessorFailure('invalid_response', reason_code='summary_support_stale')
                runner, request, executable = self.generation_fixture(runtime, dict(mode='invalid_json'),
                    failure_review=lambda failure, remaining: 'Return complete replacement JSON.', before_dispatch=guard)
                result = runner.execute(request)
                self.assertEqual('summary_support_stale', result.failure.reason_code)
                self.assertEqual(2, len(guards))
                self.assertEqual(1, len(self.submitted_generations(executable)))
                self.assertEqual(12, result.usage['tokens']['total_tokens'])
                self.assertEqual(1, len(result.metrics['generations']))
                self.assertEqual('invalid_json', result.metrics['generations'][0]['reason_code'])

    def test_external_json_replacement_retains_each_generation_and_measured_totals(self):
        for runtime in ('codex','pi'):
            for last_mode in ('valid','provider_error'):
                with self.subTest(runtime=runtime, last_mode=last_mode):
                    output = {'result': {'notes': [], 'disposition': 'skipped', 'session_summary': None}}
                    runner, request, executable = self.generation_fixture(runtime,
                        dict(modes=['invalid_json', last_mode], output=output),
                        failure_review=lambda failure, remaining: 'Return one complete replacement JSON.')
                    result = runner.execute(request)
                    self.assertEqual(2, len(self.submitted_generations(executable)))
                    self.assertEqual(24, result.usage['tokens']['total_tokens'])
                    details = result.metrics['generations']
                    self.assertEqual([12,12], [row['usage']['tokens']['total_tokens'] for row in details])
                    self.assertEqual('invalid_json', details[0]['reason_code'])
                    self.assertNotEqual(details[0]['turn_id'], details[1]['turn_id'])
                    self.assertEqual(details[0]['execution_id'], details[1]['execution_id'])
                    if last_mode=='valid':
                        self.assertIsNone(result.failure)
                        self.assertEqual(output, result.output)
                        self.assertEqual('success', details[1]['outcome'])
                    else:
                        self.assertIsNone(result.output)
                        self.assertEqual('failed', details[1]['outcome'])
                        self.assertEqual(result.failure.reason_code, details[1]['reason_code'])
