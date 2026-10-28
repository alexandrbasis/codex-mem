"""Captured observations and summaries through the real Pi execution boundary."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem.hooks import handle_hook
from codex_mem.processor import process_pending
from codex_mem.store import Store


# Real synthetic executable/review allowance; not a production or latency claim.
REAL_FIXTURE_ATTEMPT_BUDGET = 30
READINESS_WATCHDOG = REAL_FIXTURE_ATTEMPT_BUDGET + 10
OUTER_WATCHDOG = 3 * REAL_FIXTURE_ATTEMPT_BUDGET
USAGE_ACK_WATCHDOG = 20


class PiObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.data = self.root / 'memory'
        self.pi = self.root / 'pi'
        fixture = (Path(__file__).parent / 'fixtures/execution_pi.py').read_text()
        boundary = "        if mode == 'timeout':\n            time.sleep(30)"
        self.assertEqual(1, fixture.count(boundary))
        # Only this disposable copy acknowledges accepted usage before fault output.
        ack = """        if scenario.get('usage_ack'):
            acknowledgement = Path(scenario['usage_ack'])
            acknowledgement_deadline = time.monotonic() + {watchdog}
            while not acknowledgement.exists() and time.monotonic() < acknowledgement_deadline:
                time.sleep(.01)
            if not acknowledgement.exists():
                raise RuntimeError('synthetic usage acknowledgement was not received')
""".format(watchdog=USAGE_ACK_WATCHDOG)
        fixture = fixture.replace(boundary, ack + boundary, 1)
        self.pi.write_text('#!' + sys.executable + '\n' + fixture)
        self.pi.chmod(0o700)
        # One shared attempt can launch generation plus five mandatory review processes.
        # Dedicated timeout tests below retain their explicit three-second deadline.
        self.profile = dict(runtime='pi', provider='fixture', model='fixture', reasoning_effort='low',
                            timeout=REAL_FIXTURE_ATTEMPT_BUDGET)
        configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                  processing_profile=self.profile)
        self.environment = mock.patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                                           PI_CODING_AGENT_DIR=str(self.root / 'native'))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.scenario()
        self.capture()

    def scenario(self, **values):
        self.pi.with_suffix('.json').write_text(json.dumps(dict(observation=True, **values)))

    def capture(self, stop=False):
        with Store(self.data) as store:
            return handle_hook(dict(hook_event_name='Stop' if stop else 'UserPromptSubmit', cwd=str(self.project),
                session_id='session', turn_id='stop' if stop else 'turn',
                **({'last_assistant_message': 'Local import remains requested.'} if stop else {'prompt': 'Use local import.'})), store)

    def process(self, **kwargs):
        return process_pending(self.project, self.data, **kwargs)

    def generation_prompts(self):
        rows = [json.loads(line) for line in self.pi.with_suffix('.trace').read_text().splitlines()]
        return sum(row['type'] == 'prompt' and (not row['message'].startswith('Output schema: ') or
            'version' not in json.loads(row['message'].split('\n\n', 1)[0].removeprefix(
                'Output schema: ')).get('properties', {})) for row in rows)

    def attempts(self, result):
        with Store(self.data) as store:
            return store.observation_attempts(self.project, result['job_id'])

    def test_missing_auth_fails_before_prompt_and_preserves_sources(self):
        self.scenario(auth_status='not_ready')
        result = self.process()
        self.assertEqual('failed', result['status'], result)
        with Store(self.data) as store:
            job = store.observation_job_status(self.project, result['job_id'])
            self.assertEqual(1, job['source_count'])
            self.assertEqual([], store.search(self.project, 'local import'))
            receipts = store.observation_job_status(self.project, result['job_id'])['failure_receipts']
        self.assertEqual('authentication_unavailable', receipts[-1]['reason_code'])
        trace = self.pi.with_suffix('.trace')
        self.assertFalse(trace.exists() and '"type": "prompt"' in trace.read_text())

    def test_capture_pi_observation_summary_and_context_delivery(self):
        result = self.process()
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(1, result['note_count'])
        attempt = self.attempts(result)[0]
        self.assertEqual('pi', attempt['actual_runtime'])
        self.assertEqual(24, attempt['total_tokens'])
        self.assertEqual('pi_runtime_total', attempt['usage_source'])
        evidence = json.loads(attempt['execution_receipt'])
        self.assertTrue(evidence['settled'])
        self.assertNotIn('thread_start', evidence)
        self.capture(stop=True)
        summary = self.process()
        self.assertEqual('processed', summary['status'], summary)
        self.assertEqual(1, summary['session_summary_count'])
        with Store(self.data) as store:
            delivered = handle_hook(dict(hook_event_name='SessionStart', source='startup',
                cwd=str(self.project), session_id='reader'), store)
            self.assertIn('local import', json.dumps(delivered).lower())
        configure(self.data, processing_profile=None)
        self.assertEqual('idle', self.process()['status'])

    def diagnostics(self, result=None):
        with Store(self.data) as store:
            jobs = store.status(self.project)['observation_jobs']['recent']
            return dict(receipt=result, jobs=[dict(job=store.observation_job_status(self.project, row['job_id']),
                attempts=store.observation_attempts(self.project, row['job_id'])) for row in jobs])

    def assert_fault_is_atomic_and_recovery_commits(self, mode):
        import time
        from types import SimpleNamespace
        from contextlib import ExitStack
        from codex_mem import pi_runtime
        acknowledgement = self.root / 'accepted-usage'
        self.scenario(mode=mode, usage_ack=str(acknowledgement))
        accepted = []
        original_runner = pi_runtime.PiProcessorRunner
        started = time.monotonic()
        clock = [started]
        watchdog = [False]
        def runtime_monotonic():
            if time.monotonic() - started >= OUTER_WATCHDOG:
                watchdog[0] = True
                return time.monotonic() + REAL_FIXTURE_ATTEMPT_BUDGET + 1
            return clock[0]
        def acknowledged_runner(*args, **kwargs):
            original_checkpoint = kwargs.get('usage_checkpoint')
            def checkpoint(receipt):
                if original_checkpoint is not None:
                    original_checkpoint(receipt)
                if not accepted and (receipt['metrics']['usage'].get('tokens') or {}).get('total_tokens') == 12:
                    accepted.append(receipt)
                    acknowledgement.touch()
                    if mode == 'timeout':
                        clock[0] = time.monotonic() + REAL_FIXTURE_ATTEMPT_BUDGET + 1
            kwargs['usage_checkpoint'] = checkpoint
            return original_runner(*args, **kwargs)
        try:
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(pi_runtime, 'PiProcessorRunner', acknowledged_runner))
                if mode == 'timeout':
                    # The ack wait bounds a missing measurement; expire only after accepted12.
                    stack.enter_context(mock.patch.object(pi_runtime, 'time', SimpleNamespace(monotonic=runtime_monotonic)))
                failed = self.process()
        finally:
            acknowledgement.touch()
        diagnostics = self.diagnostics(failed)
        self.assertFalse(watchdog[0], dict(precondition='accepted-usage watchdog expired', diagnostics=diagnostics))
        self.assertEqual(1, len(accepted), diagnostics)
        self.assertEqual('failed', failed['status'], diagnostics)
        attempt = next(row for row in self.attempts(failed) if row['attempt_count'] == 1)
        evidence = json.loads(attempt['execution_receipt'])
        self.assertEqual(2 if mode == 'invalid_json' else 1, evidence['generation'], diagnostics)
        self.assertGreater(attempt['usage_updates'], 0, diagnostics)
        self.assertEqual(24 if mode == 'invalid_json' else 12, attempt['total_tokens'], diagnostics)
        self.assertEqual('failed', attempt['outcome'], diagnostics)
        if mode in ('overflow', 'stderr_overflow'):
            self.assertEqual('pi_output_limit', failed['reason_code'], diagnostics)
        elif mode == 'cancelled':
            self.assertEqual('pi_cancelled', failed['reason_code'], diagnostics)
        elif mode == 'timeout':
            self.assertEqual('timeout', failed['code'], diagnostics)
        with Store(self.data) as store:
            job = store.observation_job_status(self.project, failed['job_id'])
            self.assertEqual([], job['output_ids'], diagnostics)
            self.assertEqual([], store.search(self.project, 'local import'), diagnostics)
            self.assertEqual(1, job['source_count'], diagnostics)
            raw = store.timeline(self.project, limit=10)[0]
            self.assertIsNone(raw['superseded_by'], diagnostics)
        launch = json.loads(self.pi.with_suffix('.launch').read_text().splitlines()[-1])
        self.assertFalse(Path(launch['cwd']).exists(), diagnostics)
        with self.assertRaises(ProcessLookupError):
            os.kill(launch['pid'], 0)
        self.scenario()
        recovery = self.process(retry_failed=True)
        diagnostics = self.diagnostics(recovery)
        self.assertEqual('processed', recovery['status'], diagnostics)
        attempt = self.attempts(recovery)[-1]
        calls = json.loads(attempt['execution_calls'])
        self.assertEqual({'generation', 'summary_support_1'}, set(calls), diagnostics)
        self.assertTrue(all(row['outcome'] == 'success' for row in calls.values()), diagnostics)
        self.assertEqual(24, attempt['total_tokens'], diagnostics)
        with Store(self.data) as store:
            self.assertEqual(1, len(store.observation_job_status(self.project, recovery['job_id'])['output_ids']), diagnostics)

    def test_invalid_json_fault_is_atomic_and_recovery_commits(self):
        self.assert_fault_is_atomic_and_recovery_commits('invalid_json')

    def test_schema_fault_is_atomic_and_recovery_commits(self):
        self.assert_fault_is_atomic_and_recovery_commits('schema')

    def test_unknown_ref_fault_is_atomic_and_recovery_commits(self):
        self.assert_fault_is_atomic_and_recovery_commits('unknown_ref')

    def test_provider_error_fault_is_atomic_and_recovery_commits(self):
        self.assert_fault_is_atomic_and_recovery_commits('provider_error')

    def test_cancelled_fault_is_atomic_and_recovery_commits(self):
        self.assert_fault_is_atomic_and_recovery_commits('cancelled')

    def test_tool_fault_is_atomic_and_recovery_commits(self):
        self.assert_fault_is_atomic_and_recovery_commits('tool')

    def test_rerouted_fault_is_atomic_and_recovery_commits(self):
        self.assert_fault_is_atomic_and_recovery_commits('rerouted')

    def test_disconnect_fault_is_atomic_and_recovery_commits(self):
        self.assert_fault_is_atomic_and_recovery_commits('disconnect')

    def test_timeout_fault_is_atomic_and_recovery_commits(self):
        self.assert_fault_is_atomic_and_recovery_commits('timeout')

    def test_no_settlement_fault_is_atomic_and_recovery_commits(self):
        self.assert_fault_is_atomic_and_recovery_commits('no_settlement')

    def test_overflow_fault_is_atomic_and_recovery_commits(self):
        self.assert_fault_is_atomic_and_recovery_commits('overflow')

    def test_stderr_overflow_fault_is_atomic_and_recovery_commits(self):
        self.assert_fault_is_atomic_and_recovery_commits('stderr_overflow')

    def test_malformed_fault_is_atomic_and_recovery_commits(self):
        self.assert_fault_is_atomic_and_recovery_commits('malformed')


    def test_unknown_usage_and_failed_unknown_usage_stay_null(self):
        for mode in ('provider_error', 'valid'):
            self.scenario(mode=mode, omit_usage=True)
            result = self.process(retry_failed=True)
            self.assertEqual('failed' if mode == 'provider_error' else 'processed', result['status'], result)
            attempt = self.attempts(result)[-1]
            self.assertIsNone(attempt['total_tokens'])
            self.assertIsNone(attempt['reasoning_output_tokens'])
            self.assertEqual('unavailable', attempt['usage_status'])

    def test_exact_failed_packet_recovery_uses_new_attempt_profile(self):
        from codex_mem.processor import recover_failed_batch
        self.scenario(mode='provider_error')
        failed = self.process()
        self.assertEqual('failed', failed['status'], failed)
        configure(self.data, observation_profile={**self.profile, 'model': 'fixture-next'})
        self.scenario(models=[{'id': 'fixture-next', 'provider': 'fixture'}])
        result = recover_failed_batch(self.project, self.data, job_id=failed['job_id'],
            expected_error_code=failed['code'], expected_attempt_count=1)
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(failed['job_id'], result['job_id'])
        attempts = self.attempts(result)
        self.assertEqual(['fixture', 'fixture-next'], [a['requested_model'] for a in attempts])
        self.assertEqual(['fixture', 'fixture-next'], [a['actual_model'] for a in attempts])
        self.assertEqual([12, 24], [a['total_tokens'] for a in attempts])
        again = recover_failed_batch(self.project, self.data, job_id=failed['job_id'],
            expected_error_code=failed['code'], expected_attempt_count=1)
        self.assertEqual('recovery_unavailable', again['code'])

    def test_summary_quality_correction_retains_claimed_profile_and_cumulative_usage(self):
        from tests.test_jev_quality import response
        initial = self.process()
        self.assertEqual('processed', initial['status'], self.diagnostics(initial))
        self.capture(stop=True)
        configure(self.data, jev_quality_enabled=True)
        self.scenario(config_change={'path': str(self.data / 'config.json'),
            'values': {'processing_profile': None}})
        calls = []
        def evaluate(payload):
            calls.append(1)
            prompts = self.generation_prompts()
            return response(payload, .5, .1) if prompts == 2 else response(payload)
        result = self.process(jev_quality_evaluator=evaluate)
        self.assertEqual('processed', result['status'], result)
        self.assertGreaterEqual(len(calls), 3)
        attempt = self.attempts(result)[0]
        self.assertEqual('fixture', attempt['requested_model'])
        self.assertEqual(84, attempt['total_tokens'])
        self.assertEqual('generation-2', attempt['worker_turn_id'])
        evidence = json.loads(attempt['execution_receipt'])
        self.assertEqual(2, evidence['generation'])
        trace = [json.loads(line) for line in self.pi.with_suffix('.trace').read_text().splitlines()]
        self.assertEqual(9, sum(row['type'] == 'prompt' for row in trace))
        self.assertEqual(8, len(self.pi.with_suffix('.launch').read_text().splitlines()))
        self.assertEqual('idle', self.process()['status'])

    def test_one_correction_unknown_partial_and_failed_cumulative_usage(self):
        from codex_mem.pi_runtime import PiProcessorRunner
        from tests.test_summary_support import supported_review
        from codex_mem.execution import ExecutionIdentity, ExecutionResult
        class ZeroCostSupport:
            # Isolate generation usage/correction arithmetic from the required review.
            def execute(self, request):
                profile = request.profile
                return ExecutionResult(output=supported_review(request.context)['output'],
                    identity=ExecutionIdentity('pi', profile.provider, profile.model,
                        profile.reasoning_effort, 'support-fixture', 'review-1'),
                    metrics=dict(duration_ms=0, usage=dict(status='reported', source='pi_assistant_usage', updates=1,
                        tokens=dict(input_tokens=0, output_tokens=0, total_tokens=0))))
        for scenario, expected, status, succeeds in (
                ({}, 24, 'reported', True),
                ({'first_usage_unknown': True}, 12, 'partial', True),
                ({'last_usage_unknown': True}, 12, 'partial', True),
                ({'modes': ['valid', 'provider_error']}, 24, 'reported', False)):
            with self.subTest(scenario=scenario):
                calls = []
                remaining_times = []
                def review(_, remaining):
                    calls.append(1)
                    remaining_times.append(remaining)
                    return 'Use only the original supplied evidence.' if len(calls) == 1 else None
                self.scenario(**scenario)
                result = self.process(retry_failed=True, runner=PiProcessorRunner(candidate_review=review),
                    summary_reviewer=ZeroCostSupport())
                self.assertEqual('processed' if succeeds else 'failed', result['status'], result)
                attempt = self.attempts(result)[-1]
                self.assertEqual(expected, attempt['total_tokens'])
                self.assertEqual(status, attempt['usage_status'])
                self.assertEqual(2, json.loads(attempt['execution_receipt'])['generation'])
                if succeeds:
                    self.assertLess(remaining_times[1], remaining_times[0])
                    # A fresh captured event in another session is a new logical batch.
                    with Store(self.data) as store:
                        handle_hook(dict(hook_event_name='UserPromptSubmit', cwd=str(self.project),
                            session_id='session-' + str(len(self.pi.with_suffix('.launch').read_text().splitlines())),
                            turn_id='turn', prompt='Use local import.'), store)

    def test_repeated_correction_rejects_after_two_generations(self):
        from codex_mem.pi_runtime import PiProcessorRunner
        result = self.process(runner=PiProcessorRunner(candidate_review=lambda *_: 'Correct supplied input.'))
        self.assertEqual('jev_quality_uncertain', result['reason_code'], result)
        attempt = self.attempts(result)[0]
        self.assertEqual(24, attempt['total_tokens'])
        self.assertEqual(2, json.loads(attempt['execution_receipt'])['generation'])
        with Store(self.data) as store:
            self.assertEqual([], store.search(self.project, 'local import'))

    def test_source_deletion_during_pi_generation_cancels_atomic_commit(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        gate = self.root / 'release'
        self.scenario(gate=str(gate))
        ready = False
        phase = None
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.process)
            try:
                deadline = time.monotonic() + READINESS_WATCHDOG
                while time.monotonic() < deadline and not future.done():
                    if gate.with_suffix('.ready').exists():
                        phase = self.diagnostics()
                        attempts = phase['jobs'][0]['attempts'] if phase['jobs'] else []
                        if attempts and attempts[-1]['total_tokens'] == 12:
                            ready = True
                            configure(self.data, processing_profile=None)
                            with Store(self.data) as store:
                                source = store.timeline(self.project, limit=10)[0]
                                store.forget(self.project, ids=[source['id']])
                            break
                    time.sleep(.01)
            finally:
                gate.touch()
                result = future.result(timeout=OUTER_WATCHDOG)
                final = self.diagnostics(result)
        self.assertTrue(ready, dict(phase=phase, final=final))
        self.assertEqual('failed', result['status'], result)
        self.assertEqual('pi', result['execution_profile']['runtime'])
        self.assertEqual(12, self.attempts(result)[0]['total_tokens'])
        self.assertEqual(1, self.generation_prompts())
        self.assertEqual(1, len(self.pi.with_suffix('.launch').read_text().splitlines()))
        with Store(self.data) as store:
            self.assertEqual([], store.search(self.project, 'local import'))

    def test_expired_lease_reclaims_exact_job_with_current_profile(self):
        from datetime import datetime, timedelta, timezone
        from codex_mem.processor import PROCESSOR_ID
        with Store(self.data) as store:
            claimed = store.claim_observation_batch(self.project, PROCESSOR_ID, 'old-model', 'low',
                execution_profile={**self.profile, 'model': 'old-model'}, lease_seconds=1)
        future = datetime.now(timezone.utc) + timedelta(minutes=1)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return future if tz else future.replace(tzinfo=None)
        with mock.patch('codex_mem.store.datetime', Clock):
            result = self.process()
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(claimed['job_id'], result['job_id'])
        attempts = self.attempts(result)
        self.assertEqual(['lease_expired', 'processed'], [a['outcome'] for a in attempts])
        self.assertEqual(['old-model', 'fixture'], [a['requested_model'] for a in attempts])
        self.assertIsNone(attempts[0]['actual_model'])
        self.assertIsNone(attempts[0]['total_tokens'])
        self.assertEqual(24, attempts[1]['total_tokens'])

    def test_deadline_includes_version_probe(self):
        configure(self.data, processing_profile={**self.profile, "timeout": 3})
        import time
        from codex_mem.pi_runtime import PiProcessorRunner
        self.scenario(version_timeout=True)
        started = time.monotonic()
        result = self.process(runner=PiProcessorRunner())
        self.assertEqual('timeout', result['code'], result)
        self.assertLess(time.monotonic() - started, 5)
        attempt = self.attempts(result)[0]
        self.assertIsNone(attempt['total_tokens'])
        self.assertEqual(0, attempt['usage_updates'])

    def test_deadline_includes_correction_and_preserves_accepted_usage(self):
        import time
        from types import SimpleNamespace
        from codex_mem import pi_runtime
        self.scenario(modes=['valid', 'timeout'])
        started = time.monotonic()
        clock = [started]
        watchdog = [False]
        accepted = []
        original_runner = pi_runtime.PiProcessorRunner

        def runtime_monotonic():
            if time.monotonic() - started >= READINESS_WATCHDOG:
                watchdog[0] = True
                return time.monotonic() + REAL_FIXTURE_ATTEMPT_BUDGET + 1
            return clock[0]

        def acknowledged_runner(*args, **kwargs):
            original_checkpoint = kwargs.get('usage_checkpoint')

            def checkpoint(receipt):
                if original_checkpoint is not None:
                    original_checkpoint(receipt)
                tokens = (receipt['metrics']['usage'].get('tokens') or {}).get('total_tokens')
                if tokens in (12, 24) and tokens not in accepted:
                    accepted.append(tokens)
                    # The correction must retain the original 30-second deadline.
                    # Resetting it at generation two would leave time after +31.
                    clock[0] = started + (20 if tokens == 12 else 31)

            kwargs['usage_checkpoint'] = checkpoint
            return original_runner(*args, **kwargs)

        with mock.patch.object(pi_runtime, 'time', SimpleNamespace(monotonic=runtime_monotonic)):
            result = self.process(runner=acknowledged_runner(candidate_review=lambda *_: 'Correct input.'))
        diagnostics = self.diagnostics(result)
        self.assertFalse(watchdog[0], diagnostics)
        self.assertEqual([12, 24], accepted, diagnostics)
        self.assertEqual('timeout', result['code'], diagnostics)
        attempt = self.attempts(result)[0]
        self.assertEqual(24, attempt['total_tokens'], diagnostics)
        self.assertEqual(2, json.loads(attempt['execution_receipt'])['generation'], diagnostics)

    def test_mixed_processor_profiles_remain_independent_of_host_model(self):
        from codex_mem.episode_processor import process_project_memory
        from codex_mem.project_memory import ProjectMemory
        codex = self.root / 'codex'
        codex.write_text('#!' + sys.executable + '\n' + (Path(__file__).parent / 'fixtures/execution_codex.py').read_text())
        codex.chmod(0o700)
        native = dict(runtime='codex', provider='openai', model='gpt-6-luna', reasoning_effort='medium', timeout=REAL_FIXTURE_ATTEMPT_BUDGET)
        for observation_pi in (True, False):
            with self.subTest(observation_pi=observation_pi):
                self.data = self.root / ('mixed-' + str(observation_pi))
                configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                    project_memory_mode='pilot', project_memory_projects=[str(self.project)],
                    processing_profile=self.profile,
                    observation_profile=self.profile if observation_pi else native,
                    project_memory_profile=native if observation_pi else self.profile)
                with Store(self.data) as store:
                    handle_hook(dict(hook_event_name='UserPromptSubmit', cwd=str(self.project), session_id='session',
                        turn_id='turn', model='unrelated-interactive-model', prompt='Use local import.'), store)
                self.scenario()
                codex.with_suffix('.json').write_text('{}')
                observation = self.process()
                self.assertEqual('processed' if observation_pi else 'skipped', observation['status'], observation)
                self.pi.with_suffix('.json').write_text('{}')
                codex.with_suffix('.json').write_text(json.dumps({'mode': 'project_memory'}))
                pm = process_project_memory(self.project, self.data)
                self.assertEqual('processed', pm['status'], pm)
                attempt = self.attempts(observation)[0]
                self.assertEqual('pi' if observation_pi else 'codex', attempt['actual_runtime'])
                with Store(self.data) as store:
                    attempts = ProjectMemory(store).attempts(self.project)
                    self.assertEqual('codex' if observation_pi else 'pi', attempts[0]['runtime'])
                    self.assertIn('Use local import.', ProjectMemory(store).snapshot(self.project)['episodes'][0]['summary'])
                configure(self.data, processing_profile=None, observation_profile=None, project_memory_profile=None)
                self.assertEqual('idle', self.process()['status'])
                self.assertEqual('idle', process_project_memory(self.project, self.data)['status'])

    def test_retained_service_reloads_profile_for_same_job_retry(self):
        configure(self.data, processing_profile={**self.profile, "timeout": 3})
        import time
        from codex_mem.service import enqueue, run_service
        configure(self.data, service_enabled=True)
        next_profile = {**self.profile, 'model': 'fixture-next'}
        self.scenario(mode='timeout', config_change={'path': str(self.data / 'config.json'),
            'values': {'observation_profile': next_profile}},
            next_scenario={'observation': True, 'models': [{'id': 'fixture-next', 'provider': 'fixture'}]})
        now = [time.time()]
        enqueue(self.project, self.data, clock=lambda: now[0])
        def sleep(delay):
            now[0] += max(delay, 40)
        with mock.patch('time.time', side_effect=lambda: now[0]):
            result = run_service(self.data, clock=lambda: now[0], sleeper=sleep,
                poll_interval=1, retry_backoff=1, max_cycles=5, processor_workers=1,
                processor_timeout=REAL_FIXTURE_ATTEMPT_BUDGET)
        with Store(self.data) as store:
            jobs = store.status(self.project)['observation_jobs']['recent']
            self.assertEqual(1, len(jobs), result)
            attempts = store.observation_attempts(self.project, jobs[0]['job_id'])
            self.assertEqual(['failed', 'processed'], [a['outcome'] for a in attempts], attempts)
            self.assertEqual(['fixture', 'fixture-next'], [a['requested_model'] for a in attempts])
            self.assertEqual([12, 24], [a['total_tokens'] for a in attempts])


    def test_pi_requires_strict_wrapper_and_complete_note_schema(self):
        malformed = [
            {'notes': [], 'disposition': 'skipped', 'session_summary': None},
            {'result': {'notes': [], 'disposition': 'skipped'}},
            {'result': {'notes': [{'title': 'Missing metadata', 'body': 'Local import.',
                'tags': [], 'source_ids': ['s1']}], 'disposition': 'processed', 'session_summary': None}},
        ]
        for output in malformed:
            self.scenario(output=output)
            result = self.process(retry_failed=True)
            self.assertEqual('invalid_response', result['code'], result)
            self.assertEqual(12, self.attempts(result)[-1]['total_tokens'])
            with Store(self.data) as store:
                self.assertEqual([], store.observation_job_status(self.project, result['job_id'])['output_ids'])

    def test_unsupported_pi_configuration_never_sends_prepared_input(self):
        for scenario, reason in (({'version': '1.3.0'}, 'unsupported_pi_version'),
                ({'models': []}, 'exact_model_unavailable'), ({'levels': ['off']}, 'unsupported_reasoning'),
                ({'clamped': 'off'}, 'pi_identity_mismatch')):
            self.scenario(**scenario)
            result = self.process(retry_failed=True)
            self.assertEqual(reason, result['reason_code'], result)
            self.assertIsNone(self.attempts(result)[-1]['total_tokens'])
        trace = [json.loads(line) for line in self.pi.with_suffix('.trace').read_text().splitlines()]
        self.assertNotIn('prompt', [row['type'] for row in trace])

    def test_late_pi_receipt_after_lease_recovery_keeps_unknown_counters_null(self):
        from datetime import datetime, timedelta, timezone
        from tests.test_jev_quality import response
        future = datetime.now(timezone.utc) + timedelta(days=1)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return future if tz else future.replace(tzinfo=None)
        measured = {'input': 10, 'output': 2, 'cacheRead': 0, 'cacheWrite': 0, 'totalTokens': 12}
        cases = [('absent', {}, None, 24),
                 ('zero', {'usage': {**measured, 'reasoning': 0}}, 0, 24),
                 ('correction', {'usages': [dict(measured, reasoning=2), measured]}, None, 84)]
        for name, scenario, expected_reasoning, expected_total in cases:
            with self.subTest(case=name):
                self.data = self.root / ('late-' + name)
                configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                    processing_profile=self.profile)
                self.capture()
                correction = name == 'correction'
                if correction:
                    self.scenario()
                    initial = self.process()
                    self.assertEqual('processed', initial['status'], self.diagnostics(initial))
                    self.capture(stop=True)
                configure(self.data, jev_quality_enabled=True)
                self.scenario(**scenario)
                trace_path = self.pi.with_suffix('.trace')
                baseline = self.generation_prompts() if trace_path.exists() else 0
                replacement = []
                before_late_receipt = []
                first_generation = []
                def evaluate(payload):
                    prompts = self.generation_prompts()
                    if correction and prompts == baseline + 1:
                        with Store(self.data) as store:
                            job = store.status(self.project)['observation_jobs']['recent'][0]
                            first_generation.extend(store.observation_attempts(self.project, job['job_id']))
                        return response(payload, .5, .1)
                    with Store(self.data) as store:
                        current_job = store.status(self.project)['observation_jobs']['recent'][0]
                        current_attempt = store.observation_attempts(self.project, current_job['job_id'])[-1]
                    ledger = json.loads(current_attempt['execution_calls'])
                    sequence = ['summary_support_1', 'summary_source_1', 'summary_outcome_1',
                                'summary_support_2', 'summary_outcome_2'] if correction else ['summary_support_1']
                    self.assertTrue(all(ledger.get(key, {}).get('outcome') == 'success' for key in sequence),
                                    self.diagnostics())
                    statuses = ['supported', 'interpreted', 'unused', 'supported', 'unused'] if correction else ['supported']
                    self.assertEqual(statuses, [ledger[key]['review_diagnostic']['status'] for key in sequence],
                                     self.diagnostics())
                    configure(self.data, jev_quality_enabled=False,
                        observation_profile={**self.profile, 'model': 'fixture-next'})
                    self.scenario(models=[{'id': 'fixture-next', 'provider': 'fixture'}])
                    with mock.patch('codex_mem.store.datetime', Clock):
                        replacement.append(self.process())
                    before_late_receipt.extend(self.attempts(replacement[0]))
                    return response(payload)
                result = self.process(jev_quality_evaluator=evaluate)
                self.assertEqual(1, len(replacement), self.diagnostics(result))
                self.assertEqual('processed', replacement[0]['status'], replacement)
                self.assertEqual(result['job_id'], replacement[0]['job_id'])
                self.assertEqual('lease_expired', before_late_receipt[0]['outcome'])
                self.assertEqual('partial', before_late_receipt[0]['usage_status'])
                self.assertEqual(expected_reasoning, before_late_receipt[0]['reasoning_output_tokens'])
                if correction:
                    self.assertTrue(first_generation)
                    self.assertEqual(2, first_generation[0]['reasoning_output_tokens'])
                attempts = self.attempts(result)
                self.assertEqual(['lease_expired', 'processed'], [a['outcome'] for a in attempts])
                self.assertEqual(['fixture', 'fixture-next'], [a['requested_model'] for a in attempts])
                self.assertEqual([expected_total, 48 if correction else 24], [a['total_tokens'] for a in attempts])
                self.assertEqual(expected_reasoning, attempts[0]['reasoning_output_tokens'])
                self.assertIsNone(attempts[1]['reasoning_output_tokens'])
                self.assertEqual('reported', attempts[0]['usage_status'])
                with Store(self.data) as store:
                    outputs = store.observation_job_status(self.project, result['job_id'])['output_ids']
                    self.assertEqual(1, len(outputs))
                self.assertEqual('idle', self.process()['status'])

    def test_shared_deadline_expires_during_mandatory_review_without_partial_commit(self):
        import time
        from types import SimpleNamespace
        from codex_mem import processor, pi_runtime
        initial = self.process()
        self.assertEqual('processed', initial.get('status'), self.diagnostics(initial))
        initial_attempt = self.attempts(initial)[0]
        initial_calls = json.loads(initial_attempt['execution_calls'])
        self.assertEqual({'generation', 'summary_support_1'}, set(initial_calls), self.diagnostics(initial))
        self.assertEqual([12, 12], [row['metrics']['total_tokens'] for row in initial_calls.values()],
                         self.diagnostics(initial))
        self.assertTrue(all(row['outcome'] == 'success' for row in initial_calls.values()), self.diagnostics(initial))
        with Store(self.data) as store:
            initial_outputs = store.observation_job_status(self.project, initial['job_id'])['output_ids']
            self.assertEqual(1, len(initial_outputs), self.diagnostics(initial))
            before_stop_ids = {row['id'] for row in store.timeline(self.project)}
        self.capture(stop=True)
        with Store(self.data) as store:
            current_ids = {row['id'] for row in store.timeline(self.project)}
            stop_ids = current_ids - before_stop_ids
            self.assertEqual(1, len(stop_ids))
            stop_records = store.get(self.project, list(stop_ids))
            self.assertEqual('assistant_report', stop_records[0]['provenance']['record_role'])
            stop_before = {row['id']: {key: row[key] for key in ('body', 'source', 'source_ids',
                'superseded_by', 'session_id', 'provenance')} for row in stop_records}
        clock = [time.monotonic()]
        requests = []
        class Reviewer:
            def execute(inner, request):
                requests.append(request)
                result = pi_runtime.PiProcessorRunner().execute(request)
                if request.context['version'] == 'summary-outcome-v6':
                    self.assertIsNone(result.failure, result)
                    self.assertIsNotNone(result.identity, result)
                    clock[0] = request.deadline + 1
                return result
        runtime_time = SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
        with mock.patch.object(processor, 'time', runtime_time):
            result = self.process(summary_reviewer=Reviewer())
        diagnostics = self.diagnostics(result)
        self.assertEqual('failed', result.get('status'), diagnostics)
        self.assertNotEqual(initial['job_id'], result.get('job_id'), diagnostics)
        self.assertEqual(['summary-support-v7', 'summary-source-v4', 'summary-outcome-v6'],
                         [row.context['version'] for row in requests], diagnostics)
        self.assertEqual('summary_support_unavailable', result.get('reason_code'), diagnostics)
        self.assertEqual(1, len({row.deadline for row in requests}), diagnostics)
        attempt = self.attempts(result)[0]
        calls = json.loads(attempt['execution_calls'])
        self.assertEqual({'generation', 'summary_support_1', 'summary_source_1', 'summary_outcome_1'}, set(calls), diagnostics)
        self.assertEqual([12] * 4, [row['metrics']['total_tokens'] for row in calls.values()], diagnostics)
        self.assertEqual(48, attempt['total_tokens'], diagnostics)
        self.assertEqual('unavailable', result['summary_support'][-1]['status'], diagnostics)
        with Store(self.data) as store:
            job = store.observation_job_status(self.project, result['job_id'])
            self.assertEqual([], job['output_ids'], diagnostics)
            self.assertEqual(len(stop_ids), job['source_count'], diagnostics)
            self.assertEqual(current_ids, {row['id'] for row in store.timeline(self.project)}, diagnostics)
            self.assertEqual(initial_outputs,
                store.observation_job_status(self.project, initial['job_id'])['output_ids'], diagnostics)
            self.assertEqual(set(initial_outputs), {row['id'] for row in store.search(self.project, 'local import')}, diagnostics)
            stop_after = {row['id']: {key: row[key] for key in ('body', 'source', 'source_ids',
                'superseded_by', 'session_id', 'provenance')} for row in store.get(self.project, list(stop_ids))}
            self.assertEqual(stop_before, stop_after, diagnostics)
            self.assertTrue(all(row['superseded_by'] is None for row in stop_after.values()), diagnostics)

    def test_optional_usage_missing_after_correction_does_not_erase_measured_totals(self):
        from tests.test_jev_quality import response
        initial = self.process()
        self.assertEqual('processed', initial['status'], self.diagnostics(initial))
        self.capture(stop=True)
        configure(self.data, jev_quality_enabled=True)
        self.scenario(usages=[{'input': 7, 'output': 5, 'cacheRead': 3, 'cacheWrite': 2,
                               'reasoning': 2, 'totalTokens': 17},
                              {'input': 10, 'output': 2, 'cacheRead': 0, 'cacheWrite': 0, 'totalTokens': 12}])
        def evaluate(payload):
            prompts = self.generation_prompts()
            if prompts == 3:
                with Store(self.data) as store:
                    job = store.status(self.project)['observation_jobs']['recent'][0]
                    live = store.observation_attempts(self.project, job['job_id'])[-1]
                    self.assertEqual(29, live['total_tokens'])
            return response(payload, .5, .1) if prompts == 2 else response(payload)
        result = self.process(jev_quality_evaluator=evaluate)
        self.assertEqual('processed', result['status'], result)
        attempt = self.attempts(result)[0]
        self.assertEqual(114, attempt['total_tokens'])
        self.assertEqual(82, attempt['input_tokens'])
        self.assertEqual(32, attempt['output_tokens'])
        self.assertIsNone(attempt['reasoning_output_tokens'])
        self.assertEqual('reported', attempt['usage_status'])


@unittest.skipUnless(os.environ.get('CODEX_MEM_PI'), 'set CODEX_MEM_PI for installed Pi with local provider')
class InstalledPiObservationTests(unittest.TestCase):
    setUp = PiObservationTests.setUp
    scenario = PiObservationTests.scenario
    capture = PiObservationTests.capture
    process = PiObservationTests.process
    attempts = PiObservationTests.attempts

    def test_installed_pi_delivers_observation_with_native_receipt(self):
        import http.server
        import threading
        from tests.test_summary_support import supported_review
        calls = []
        contracts = []
        class Provider(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                call = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(call)
                content = call['messages'][-1]['content']
                text = content if isinstance(content, str) else '\n'.join(part['text'] for part in content)
                schema = json.loads(text.split('Output schema: ', 1)[1].split('\n\n', 1)[0])
                version = schema['properties'].get('version', {}).get('enum', [None])[0]
                contracts.append(version or 'generation')
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                output = json.dumps({'result': {'disposition': 'processed', 'session_summary': None,
                    'notes': [{'title': 'Local import', 'body': 'The user requests local import.',
                        'tags': ['import'], 'source_ids': ['s1'], 'observation': {'type': 'decision',
                        'subtitle': '', 'narrative': '', 'facts': [], 'concepts': [], 'files_read': [], 'files_modified': []}}]}})
                if version is not None:
                    # Authored support-v5 rows test provider/schema transport, not meaning.
                    assert version == 'summary-support-v7', version
                    payload = json.loads(text.split('\n')[-1])
                    output = json.dumps(supported_review(payload)['output'])
                for delta, finish in (({'role': 'assistant', 'content': output}, None), ({}, 'stop')):
                    value = {'id': 'fixture', 'object': 'chat.completion.chunk', 'model': 'fixture',
                        'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                    self.wfile.write(('data: ' + json.dumps(value) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        native = self.root / 'native'
        native.mkdir()
        (native / 'models.json').write_text(json.dumps({'providers': {'fixture': {
            'baseUrl': f'http://127.0.0.1:{server.server_port}/v1', 'api': 'openai-completions',
            'apiKey': 'fixture-not-a-credential',
            'models': [{'id': 'fixture', 'contextWindow': 128000, 'maxTokens': 1024}]}}}))
        (native / 'AGENTS.md').write_text('SECRET_TEST_INSTRUCTION_MUST_NOT_LOAD')
        (native / 'settings.json').write_text(json.dumps({'extensions': ['./untrusted.ts']}))
        (native / 'untrusted.ts').write_text('throw new Error("UNTRUSTED_EXTENSION_LOADED");')
        self.pi.unlink()
        self.pi.symlink_to(os.environ['CODEX_MEM_PI'])
        configure(self.data, processing_profile={**self.profile, 'reasoning_effort': 'none', 'timeout': 30})
        result = self.process()
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(['generation', 'summary-support-v7'], contracts)
        self.assertEqual(2, len(calls))
        for call in calls:
            self.assertFalse(call.get('tools'))
            self.assertEqual('fixture', call['model'])
        self.assertNotIn('SECRET_TEST_INSTRUCTION', json.dumps(calls))
        attempt = self.attempts(result)[0]
        self.assertIsNone(attempt['total_tokens'])
        self.assertTrue(json.loads(attempt['execution_receipt'])['settled'])
        self.assertEqual(('pi', 'fixture', 'fixture'),
            (attempt['actual_runtime'], attempt['actual_provider'], attempt['actual_model']))
        ledger = json.loads(attempt['execution_calls'])
        self.assertEqual({'generation', 'summary_support_1'}, set(ledger))
        for call in ledger.values():
            self.assertEqual('success', call['outcome'])
            self.assertEqual({'runtime': 'pi', 'provider': 'fixture', 'model': 'fixture',
                'reasoning_effort': 'none'}, call['actual_identity'])
        with Store(self.data) as store:
            delivered = handle_hook(dict(hook_event_name='SessionStart', source='startup',
                cwd=str(self.project), session_id='reader'), store)
            self.assertIn('local import', json.dumps(delivered).lower())
