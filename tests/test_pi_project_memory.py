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
        self.selected = dict(runtime='pi', provider='fixture', model='fixture', reasoning_effort='low', timeout=3)
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


    def test_failed_packets_leave_no_memory_keep_measured_usage_and_can_retry(self):
        for mode in ('invalid_json', 'schema', 'unknown_ref', 'provider_error', 'cancelled',
                     'tool', 'rerouted', 'disconnect', 'timeout', 'no_settlement', 'overflow', 'stderr_overflow', 'malformed'):
            with self.subTest(mode=mode):
                self.scenario(mode=mode)
                failed = self.process()
                self.assertEqual('failed', failed['status'], failed)
                with Store(self.data) as store:
                    memory = ProjectMemory(store)
                    self.assertEqual([], memory.snapshot(self.project)['episodes'])
                    self.assertGreater(memory.snapshot(self.project)['coverage']['pending_events'], 0)
                    attempt = memory.attempts(self.project)[0]
                    self.assertEqual(12, attempt['total_tokens'])
                    self.assertEqual('failed', attempt['outcome'])
                    if mode == 'tool':
                        self.assertEqual(1, json.loads(attempt['execution_receipt'])['tool_calls_observed'])
                launch = json.loads(self.pi.with_suffix('.launch').read_text().splitlines()[-1])
                self.assertFalse(Path(launch['cwd']).exists())
                with self.assertRaises(ProcessLookupError):
                    os.kill(launch['pid'], 0)
                self.retry()
        self.scenario()
        self.assertEqual('processed', self.process()['status'])

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
        self.assertEqual('processed', self.process()['status'])
        with Store(self.data) as store:
            attempt = ProjectMemory(store).attempts(self.project)[0]
            self.assertIsNone(attempt['total_tokens'])
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
                deadline = time.monotonic() + 3
                while not gate.with_suffix('.ready').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(gate.with_suffix('.ready').exists())
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
