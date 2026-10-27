"""Public capture -> real queue -> external Pi executable -> delivered memory."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem.episode_processor import process_project_memory
from codex_mem.hooks import handle_hook
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store


# Real disposable executable preparation is separate from deadline policy tests.
FIXTURE_ATTEMPT_SECONDS = 30
DEADLINE_TEST_SECONDS = 3
TEST_WATCHDOG_SECONDS = 30


class PiProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.data = self.root / 'memory'
        self.pi = self.root / 'pi'
        self.pi.write_text('#!' + sys.executable + '\n' + (Path(__file__).parent / 'fixtures/execution_pi.py').read_text())
        self.pi.chmod(0o700)
        self.selected = dict(runtime='pi', provider='fixture', model='fixture', reasoning_effort='low', timeout=FIXTURE_ATTEMPT_SECONDS)
        configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                  project_memory_mode='pilot', project_memory_projects=[str(self.project)],
                  project_memory_profile=self.selected)
        self.environment = mock.patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                                           PI_CODING_AGENT_DIR=str(self.root / 'native'))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.scenario()
        with Store(self.data) as store:
            handle_hook(dict(hook_event_name='UserPromptSubmit', cwd=str(self.project),
                session_id='session', turn_id='turn', prompt='Use local import.'), store)

    def scenario(self, **values):
        self.pi.with_suffix('.json').write_text(json.dumps(values))

    def process(self, **kwargs):
        return process_project_memory(self.project, self.data, **kwargs)

    def test_pi_processes_codex_event_and_delivers_memory_with_native_receipt(self):
        result = self.process()
        self.assertEqual('processed', result['status'], result)
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            self.assertIn('Use local import.', memory.snapshot(self.project)['episodes'][0]['summary'])
            attempt = memory.attempts(self.project)[0]
            self.assertEqual('pi', attempt['runtime'])
            self.assertEqual('fixture', attempt['actual_model'])
            self.assertEqual('pi_assistant_usage', attempt['usage_source'])
            self.assertEqual(12, attempt['total_tokens'])
            evidence = json.loads(attempt['execution_receipt'])
            self.assertEqual('1.4.0', evidence['version'])
            self.assertTrue(evidence['settled'])
            self.assertEqual('cli_disabled', evidence['tools_policy'])
            self.assertNotIn('thread_start', evidence)
            delivered = handle_hook(dict(hook_event_name='SessionStart', source='startup',
                cwd=str(self.project), session_id='reader'), store)
            self.assertIn('Use local import.', json.dumps(delivered))
        launch = json.loads(self.pi.with_suffix('.launch').read_text().splitlines()[0])
        self.assertNotEqual(str(self.project), launch['cwd'])
        self.assertFalse(Path(launch['cwd']).exists())
        for flag in ('--no-tools', '--no-mcp', '--no-extensions', '--no-context-files', '--no-skills', '--no-session'):
            self.assertIn(flag, launch['args'])
        configure(self.data, project_memory_profile=None)
        self.assertEqual('idle', self.process()['status'])

    def retry(self):
        with Store(self.data) as store:
            ProjectMemory(store).retry_failed(self.project, include_quarantined=True)

    def test_native_thinking_level_notification_allows_public_memory_delivery(self):
        self.scenario(thinking_notification={'type': 'thinking_level_changed', 'level': 'low'})
        result = self.process()
        self.assertEqual('processed', result['status'], result)
        with Store(self.data) as store:
            delivered = handle_hook(dict(hook_event_name='SessionStart', source='startup',
                cwd=str(self.project), session_id='reader'), store)
            self.assertIn('Use local import.', json.dumps(delivered))

    def test_thinking_notification_rejects_mismatch_extra_fields_and_other_events(self):
        for event in ({'type': 'thinking_level_changed', 'level': 'high'},
                      {'type': 'thinking_level_changed', 'level': 'low', 'extra': True},
                      {'type': 'agent_start'}):
            with self.subTest(event=event):
                self.scenario(thinking_notification=event)
                result = self.process()
                self.assertEqual('failed', result['status'], result)
                with Store(self.data) as store:
                    memory = ProjectMemory(store)
                    self.assertEqual([], memory.snapshot(self.project)['episodes'])
                    self.assertGreater(memory.snapshot(self.project)['coverage']['pending_events'], 0)
                self.retry()

    def test_unsolicited_thinking_notification_during_generation_rejects_packet(self):
        self.scenario(generation_thinking_notification=True)
        result = self.process()
        self.assertEqual('failed', result['status'], result)
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            self.assertEqual([], memory.snapshot(self.project)['episodes'])
            self.assertGreater(memory.snapshot(self.project)['coverage']['pending_events'], 0)


    def assert_failed_packet_retained(self, failed, *, total_tokens, usage_status=None, usage_updates=None):
        self.assertEqual('failed', failed['status'], failed)
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            self.assertEqual([], memory.snapshot(self.project)['episodes'])
            self.assertEqual(1, memory.snapshot(self.project)['coverage']['pending_events'])
            attempt = next(row for row in memory.attempts(self.project)
                           if row['attempt_id'] == failed['usage_attempt_id'])
            self.assertEqual(total_tokens, attempt['total_tokens'], dict(result=failed, attempt=attempt))
            self.assertEqual('failed', attempt['outcome'])
            if usage_status is not None:
                self.assertEqual(usage_status, attempt['usage_status'])
            if usage_updates is not None:
                self.assertEqual(usage_updates, attempt['usage_updates'])
        launch = json.loads(self.pi.with_suffix('.launch').read_text().splitlines()[-1])
        self.assertFalse(Path(launch['cwd']).exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(launch['pid'], 0)
        return attempt

    def assert_explicit_retry_processes(self, failed):
        # A retry tests recovery, not the deliberately short deadline above.
        self.selected['timeout'] = FIXTURE_ATTEMPT_SECONDS
        configure(self.data, project_memory_profile=self.selected)
        self.scenario()
        self.assertEqual('idle', self.process()['status'])
        self.retry()
        processed = self.process()
        self.assertEqual('processed', processed['status'], processed)
        self.assertNotEqual(failed['usage_attempt_id'], processed['usage_attempt_id'])
        with Store(self.data) as store:
            snapshot = ProjectMemory(store).snapshot(self.project)
            self.assertEqual(1, len(snapshot['episodes']))
            self.assertEqual(0, snapshot['coverage']['pending_events'])

    def assert_failed_mode_can_retry(self, mode):
        self.scenario(mode=mode)
        failed = self.process()
        attempt = self.assert_failed_packet_retained(failed, total_tokens=12)
        if mode == 'disconnect':
            self.assertEqual('pi_disconnected', failed.get('reason_code'), failed)
            evidence = json.loads(attempt['execution_receipt'])
            self.assertEqual(1, evidence['generation'], failed)
            self.assertFalse(evidence['agent_end_observed'], failed)
            self.assertEqual('reported', attempt['usage_status'], attempt)
            self.assertEqual(2, attempt['usage_updates'], attempt)
        if mode == 'tool':
            self.assertEqual(1, json.loads(attempt['execution_receipt'])['tool_calls_observed'])
        if mode == 'no_settlement':
            evidence = json.loads(attempt['execution_receipt'])
            self.assertTrue(evidence['agent_end_observed'])
            self.assertFalse(evidence['settled'])
        self.assert_explicit_retry_processes(failed)

    def test_failed_invalid_json_packet_can_retry_independently(self):
        self.assert_failed_mode_can_retry('invalid_json')

    def test_failed_schema_packet_can_retry_independently(self):
        self.assert_failed_mode_can_retry('schema')

    def test_failed_unknown_ref_packet_can_retry_independently(self):
        self.assert_failed_mode_can_retry('unknown_ref')

    def test_failed_provider_error_packet_can_retry_independently(self):
        self.assert_failed_mode_can_retry('provider_error')

    def test_failed_cancelled_packet_can_retry_independently(self):
        self.assert_failed_mode_can_retry('cancelled')

    def test_failed_tool_packet_can_retry_independently(self):
        self.assert_failed_mode_can_retry('tool')

    def test_failed_rerouted_packet_can_retry_independently(self):
        self.assert_failed_mode_can_retry('rerouted')

    def test_failed_disconnect_packet_can_retry_independently(self):
        self.assert_failed_mode_can_retry('disconnect')

    def test_failed_no_settlement_packet_can_retry_independently(self):
        self.assert_failed_mode_can_retry('no_settlement')

    def test_failed_overflow_packet_can_retry_independently(self):
        self.assert_failed_mode_can_retry('overflow')

    def test_failed_stderr_overflow_packet_can_retry_independently(self):
        self.assert_failed_mode_can_retry('stderr_overflow')

    def test_failed_malformed_packet_can_retry_independently(self):
        self.assert_failed_mode_can_retry('malformed')

    def controlled_runtime_clock(self):
        self.selected['timeout'] = DEADLINE_TEST_SECONDS
        configure(self.data, project_memory_profile=self.selected)
        import time
        from types import SimpleNamespace
        started = time.monotonic()
        clock = [started]
        watchdog = [False]
        def expire():
            clock[0] = time.monotonic() + self.selected['timeout'] + 1
        def monotonic():
            # This is a test watchdog, separate from the unchanged three-second
            # simulated production deadline. Never count watchdog expiry as proof.
            if time.monotonic() - started >= TEST_WATCHDOG_SECONDS:
                watchdog[0] = True
                expire()
            return clock[0]
        return expire, SimpleNamespace(monotonic=monotonic), watchdog

    def test_timeout_after_accepted_usage_keeps_exact_measurement_and_can_retry(self):
        from codex_mem import pi_runtime
        self.scenario(mode='timeout')
        expire, runtime_time, watchdog = self.controlled_runtime_clock()
        accepted = []
        def checkpoint(receipt):
            usage = receipt['metrics']['usage']
            if not accepted and (usage.get('tokens') or {}).get('total_tokens') == 12:
                accepted.append(usage)
                expire()
        # Hold only the runtime clock through real subprocess startup. The callback
        # expires it after real usage acceptance; the next real remaining() fails.
        with mock.patch.object(pi_runtime, 'time', runtime_time):
            failed = self.process(runner=pi_runtime.PiProcessorRunner(usage_checkpoint=checkpoint))
        self.assertFalse(watchdog[0], 'Usage acceptance precondition was not reached within the test watchdog.')
        self.assertEqual(1, len(accepted))
        self.assertEqual(12, accepted[0]['tokens']['total_tokens'])
        self.assertEqual('timeout', failed['code'], failed)
        attempt = self.assert_failed_packet_retained(failed, total_tokens=12,
                                                     usage_status='reported', usage_updates=1)
        evidence = json.loads(attempt['execution_receipt'])
        self.assertEqual(1, evidence['generation'])
        self.assertFalse(evidence['settled'])
        self.assertFalse(evidence['agent_end_observed'])
        self.assert_explicit_retry_processes(failed)

    def test_timeout_before_measurement_keeps_unknown_usage_and_can_retry(self):
        from codex_mem import pi_runtime
        self.scenario(mode='timeout')
        expire, runtime_time, watchdog = self.controlled_runtime_clock()
        prepared = []
        def checkpoint(receipt):
            if not prepared:
                prepared.append(receipt)
                expire()
        # The first real prepared-session checkpoint has no measured usage.
        # Expiry is observed by the real send/remaining path before any prompt.
        with mock.patch.object(pi_runtime, 'time', runtime_time):
            failed = self.process(runner=pi_runtime.PiProcessorRunner(usage_checkpoint=checkpoint))
        self.assertFalse(watchdog[0], 'Prepared-session precondition was not reached within the test watchdog.')
        self.assertEqual(1, len(prepared))
        self.assertIsNone(prepared[0]['metrics']['usage']['tokens'])
        self.assertEqual(0, prepared[0]['metrics']['usage']['updates'])
        self.assertEqual('timeout', failed['code'], failed)
        self.assert_failed_packet_retained(failed, total_tokens=None,
                                           usage_status='unknown', usage_updates=0)
        commands = [json.loads(line)['type'] for line in self.pi.with_suffix('.trace').read_text().splitlines()]
        self.assertNotIn('prompt', commands)
        self.assert_explicit_retry_processes(failed)

    def test_configuration_failures_never_send_prepared_input(self):
        for scenario, reason in (({'auth_status': 'not_ready'}, 'authentication_unavailable'),
                ({'auth_status': 'not_ready', 'auth_reason': 'provider_not_found'}, 'unsupported_provider'),
                ({'version': '1.3.0'}, 'unsupported_pi_version'),
                ({'models': []}, 'exact_model_unavailable'), ({'levels': ['off']}, 'unsupported_reasoning'),
                ({'clamped': 'off'}, 'pi_identity_mismatch')):
            with self.subTest(scenario=scenario):
                self.scenario(**scenario)
                failed = self.process()
                self.assertEqual(reason, failed['reason_code'], failed)
                with Store(self.data) as store:
                    attempt = ProjectMemory(store).attempts(self.project)[0]
                    self.assertIsNone(attempt['total_tokens'])
                    self.assertEqual(reason, attempt['reason_code'])
                self.retry()
        trace = self.pi.with_suffix('.trace')
        self.assertNotIn('prompt', [json.loads(line)['type'] for line in trace.read_text().splitlines()])

    def test_absent_and_zero_placeholder_usage_remain_unknown(self):
        self.scenario(omit_usage=True)
        result = self.process()
        self.assertEqual('processed', result['status'], result)
        with Store(self.data) as store:
            attempt = ProjectMemory(store).attempts(self.project)[0]
            self.assertIsNone(attempt['total_tokens'], attempt)
            self.assertEqual('unknown', attempt['usage_status'])

    def test_one_correction_keeps_profile_deadline_and_counts_each_generation_once(self):
        from codex_mem.pi_runtime import PiProcessorRunner
        reviews = []
        def review(output, remaining):
            reviews.append(remaining)
            return 'Correct only supplied evidence.' if len(reviews) == 1 else None
        result = self.process(runner=PiProcessorRunner(candidate_review=review))
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(2, len(reviews))
        self.assertLess(reviews[1], reviews[0])
        with Store(self.data) as store:
            attempt = ProjectMemory(store).attempts(self.project)[0]
            self.assertEqual(24, attempt['total_tokens'])
            self.assertEqual(2, json.loads(attempt['execution_receipt'])['generation'])
        self.assertEqual(1, len(self.pi.with_suffix('.launch').read_text().splitlines()))

    def test_partial_correction_usage_is_not_promoted_to_reported(self):
        from codex_mem.pi_runtime import PiProcessorRunner
        self.scenario(first_usage_unknown=True)
        calls = []
        def review(*_):
            calls.append(1)
            return 'Correct supplied input.' if len(calls) == 1 else None
        result = self.process(runner=PiProcessorRunner(candidate_review=review))
        self.assertEqual('processed', result['status'], result)
        with Store(self.data) as store:
            attempt = ProjectMemory(store).attempts(self.project)[0]
            self.assertEqual(12, attempt['total_tokens'])
            self.assertEqual('partial', attempt['usage_status'])

    def test_unknown_second_generation_keeps_prior_usage_partial(self):
        from codex_mem.pi_runtime import PiProcessorRunner
        self.scenario(last_usage_unknown=True)
        calls = []
        def review(*_):
            calls.append(1)
            return 'Correct supplied input.' if len(calls) == 1 else None
        def checkpoint(receipt):
            with Store(self.data) as store:
                memory = ProjectMemory(store)
                attempt = memory.attempts(self.project)[0]
                memory.checkpoint_attempt(self.project, attempt['attempt_id'], receipt)
        self.assertEqual('processed', self.process(runner=PiProcessorRunner(
            candidate_review=review, usage_checkpoint=checkpoint))['status'])
        with Store(self.data) as store:
            attempt = ProjectMemory(store).attempts(self.project)[0]
            self.assertEqual(12, attempt['total_tokens'])
            self.assertEqual('partial', attempt['usage_status'])

    def test_repeated_correction_rejects_whole_packet_with_measured_usage(self):
        from codex_mem.pi_runtime import PiProcessorRunner
        result = self.process(runner=PiProcessorRunner(candidate_review=lambda *_: 'Correct again.'))
        self.assertEqual('failed', result['status'], result)
        self.assertEqual('jev_quality_uncertain', result['reason_code'])
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            self.assertEqual([], memory.snapshot(self.project)['episodes'])
            self.assertEqual(24, memory.attempts(self.project)[0]['total_tokens'])

    def test_prompt_handled_or_queued_is_not_completion(self):
        for disposition in ('handled', 'queued'):
            self.scenario(disposition=disposition)
            result = self.process()
            self.assertEqual('failed', result['status'], result)
            self.assertEqual('pi_prompt_not_started', result['reason_code'])
            self.retry()

    def test_cached_input_normalizes_without_double_counting_reasoning(self):
        self.scenario(usage={'input': 7, 'output': 5, 'cacheRead': 3, 'cacheWrite': 2,
                             'reasoning': 2, 'totalTokens': 17})
        self.assertEqual('processed', self.process()['status'])
        with Store(self.data) as store:
            attempt = ProjectMemory(store).attempts(self.project)[0]
            self.assertEqual(12, attempt['input_tokens'])
            self.assertEqual(5, attempt['output_tokens'])
            self.assertEqual(17, attempt['total_tokens'])
            self.assertEqual(2, attempt['reasoning_tokens'])

    def test_reconsideration_consolidates_explicit_task_with_same_pi_profile(self):
        with Store(self.data) as store:
            store.forget(self.project, ids=[store.timeline(self.project, limit=10)[0]['id']])
        for session, text in (('first', 'Task JOB-17: build the importer.'),
                               ('second', 'Continue task JOB-17. Local tests passed.')):
            with Store(self.data) as store:
                handle_hook(dict(hook_event_name='UserPromptSubmit', cwd=str(self.project),
                    session_id=session, turn_id='turn', prompt=text), store)
            self.scenario(mode='pm_task')
            self.assertEqual('processed', self.process()['status'])
        self.scenario(mode='pm_consolidate')
        result = self.process()
        self.assertEqual('processed', result['status'], result)
        self.assertEqual('reconsideration', result['job_kind'])
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            self.assertEqual(1, len(memory.snapshot(self.project)['episodes']))
            self.assertEqual('pi', memory.attempts(self.project)[0]['runtime'])
            delivered = handle_hook(dict(hook_event_name='SessionStart', source='startup',
                cwd=str(self.project), session_id='reader'), store)
            self.assertIn('JOB-17', json.dumps(delivered))

    def test_active_profile_change_and_source_deletion_fence_commit(self):
        import time
        from concurrent.futures import ThreadPoolExecutor
        gate = self.root / 'release'
        self.scenario(gate=str(gate), config_change={'path': str(self.data / 'config.json'),
            'values': {'project_memory_profile': None}})
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.process)
            try:
                deadline = time.monotonic() + FIXTURE_ATTEMPT_SECONDS + 5
                while not gate.with_suffix('.ready').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(gate.with_suffix('.ready').exists(), 'Synthetic generation gate not reached before readiness watchdog.')
                with Store(self.data) as store:
                    source = store.timeline(self.project, limit=10)[0]
                    store.forget(self.project, ids=[source['id']])
            finally:
                gate.touch()
            result = future.result(timeout=5)
        self.assertEqual('deferred', result['status'], result)
        self.assertEqual('source_changed', result['code'])
        self.assertEqual('pi', result['execution_profile']['runtime'])
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            self.assertEqual([], memory.snapshot(self.project)['episodes'])
            self.assertEqual(12, memory.attempts(self.project)[0]['total_tokens'])

    def test_deadline_includes_version_probe_and_second_generation(self):
        import time
        from codex_mem.pi_runtime import PiProcessorRunner
        self.selected['timeout'] = DEADLINE_TEST_SECONDS
        configure(self.data, project_memory_profile=self.selected)
        for scenario in ({'version_timeout': True}, {'modes': ['valid', 'timeout']}):
            with self.subTest(scenario=scenario):
                self.scenario(**scenario)
                started = time.monotonic()
                result = self.process(runner=PiProcessorRunner(candidate_review=lambda *_: 'Correct input.'))
                self.assertEqual('timeout', result['code'], result)
                self.assertLess(time.monotonic() - started, 5)
                self.retry()

    def test_service_reloads_pi_profile_on_next_attempt(self):
        import time
        from codex_mem.service import run_service
        codex = self.root / 'codex'
        codex.write_text('#!' + sys.executable + '\n' + (Path(__file__).parent / 'fixtures/execution_codex.py').read_text())
        codex.chmod(0o700)
        codex.with_suffix('.json').write_text('{}')
        configure(self.data, service_enabled=True, processor_enabled=True)
        selected = {**self.selected, 'model': 'fixture-next'}
        self.scenario(mode='timeout', config_change={'path': str(self.data / 'config.json'),
            'values': {'project_memory_profile': selected}},
            next_scenario={'models': [{'id': 'fixture-next', 'provider': 'fixture'}]})
        now = [time.time() + 40]
        def sleep(delay):
            now[0] += max(delay, 40)
        with mock.patch('time.time', side_effect=lambda: now[0]):
            run_service(self.data, clock=lambda: now[0], sleeper=sleep,
                        poll_interval=1, max_cycles=4, processor_workers=1)
        with Store(self.data) as store:
            attempts = list(reversed(ProjectMemory(store).attempts(self.project)))
            self.assertEqual(['failed', 'processed'], [a['outcome'] for a in attempts])
            self.assertEqual(['fixture', 'fixture-next'], [a['actual_model'] for a in attempts])
            self.assertEqual(attempts[0]['job_id'], attempts[1]['job_id'])
        launches = [json.loads(line) for line in self.pi.with_suffix('.launch').read_text().splitlines()]
        self.assertEqual(2, len({launch['cwd'] for launch in launches}))


@unittest.skipUnless(os.environ.get('CODEX_MEM_PI'), 'set CODEX_MEM_PI for installed Pi with local provider')
class InstalledPiProjectMemoryTests(unittest.TestCase):
    setUp = PiProjectMemoryTests.setUp
    scenario = PiProjectMemoryTests.scenario
    process = PiProjectMemoryTests.process

    def test_installed_pi_uses_pinned_protocol_without_tools_or_discovered_resources(self):
        import http.server
        import threading
        calls = []
        class Provider(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                call = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(call)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                output = json.dumps({'episodes': [], 'claims': []})
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
        configure(self.data, project_memory_profile={**self.selected, 'reasoning_effort': 'none', 'timeout': 30})
        result = self.process()
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(1, len(calls))
        self.assertFalse(calls[0].get('tools'))
        self.assertNotIn('SECRET_TEST_INSTRUCTION', json.dumps(calls))
        with Store(self.data) as store:
            attempt = ProjectMemory(store).attempts(self.project)[0]
            self.assertIsNone(attempt['total_tokens'])
            self.assertEqual('pi', attempt['runtime'])
            self.assertTrue(json.loads(attempt['execution_receipt'])['settled'])
