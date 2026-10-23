"""Configured project memory through real capture, queue, runtime and retrieval."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest import mock

import test_execution_contract as fixture
from codex_mem.config import configure
from codex_mem.episode_processor import process_project_memory
from codex_mem.hooks import handle_hook
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store


def profile(model='fixture-exact', effort='low', timeout=10):
    return dict(runtime='codex', provider='openai', model=model,
                reasoning_effort=effort, timeout=timeout)


class ProjectMemoryProfileTests(unittest.TestCase):
    scenario = fixture.ExecutionContractTests.scenario

    def setUp(self):
        fixture.ExecutionContractTests.setUp(self)
        self.data = self.root / 'memory'
        self.project = self.root / 'project'
        self.project.mkdir()
        configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                  project_memory_mode='pilot', project_memory_projects=[str(self.project)])
        (self.root / 'codex').symlink_to(self.executable)
        self.environment = mock.patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'])
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def capture(self, session='session'):
        with Store(self.data) as store:
            return handle_hook(dict(hook_event_name='UserPromptSubmit', cwd=str(self.project),
                session_id=session, turn_id='turn', prompt='Use local import.'), store)

    def process(self):
        return process_project_memory(self.project, self.data, timeout=10)

    def trace(self, method):
        path = self.executable.with_suffix('.trace')
        return [row['params'] for row in map(json.loads, path.read_text().splitlines())
                if row.get('method') == method] if path.exists() else []

    def test_override_selects_profile_and_public_memory_survives_model_switch(self):
        configure(self.data, processing_profile=profile('missing'),
                  observation_profile=profile('observation-only'), project_memory_profile=profile())
        self.scenario(mode='project_memory', claim=True, models=[{'id': 'picker', 'model': 'fixture-exact',
            'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}])
        self.capture()
        result = self.process()
        self.assertEqual('processed', result['status'], result)
        self.assertEqual('fixture-exact', self.trace('turn/start')[0]['model'])
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            self.assertIn('Use local import.', memory.snapshot(self.project)['episodes'][0]['summary'])
            self.assertEqual('Use local import.', memory.snapshot(self.project)['claims'][0]['text'])
            attempt = memory.attempts(self.project)[0]
            self.assertEqual('fixture-exact', attempt['requested_model'])
            self.assertEqual('fixture-exact', attempt['actual_model'])
            self.assertEqual('codex', attempt['runtime'])
            self.assertEqual('openai', attempt['provider'])
            self.assertEqual('low', attempt['actual_reasoning_effort'])
            self.assertEqual(12, attempt['total_tokens'])
        with Store(self.data) as store:
            delivered = handle_hook(dict(hook_event_name='SessionStart', source='startup',
                cwd=str(self.project), session_id='reader'), store)
            self.assertIn('Use local import.', json.dumps(delivered))
        configure(self.data, project_memory_profile=None, processing_profile=None)
        self.scenario(mode='project_memory')
        self.assertEqual('idle', self.process()['status'])
        self.assertEqual(1, len(self.trace('turn/start')))

    def test_invalid_capabilities_keep_pending_sources_and_unknown_identity(self):
        self.capture()
        for selected, reason in ((profile('missing'), 'exact_model_unavailable'),
                                 (profile('gpt-6-luna', 'high'), 'unsupported_reasoning'),
                                 ({**profile(), 'provider': 'custom'}, 'unsupported_provider'),
                                 ({**profile(), 'runtime': 'pi'}, 'unsupported_runtime')):
            with self.subTest(reason=reason):
                configure(self.data, project_memory_profile=selected)
                result = self.process()
                self.assertEqual('failed', result['status'], result)
                self.assertEqual(reason, result['reason_code'])
                with Store(self.data) as store:
                    memory = ProjectMemory(store)
                    state = memory.snapshot(self.project)
                    self.assertEqual([], state['episodes'])
                    self.assertGreater(state['coverage']['pending_events'], 0)
                    attempt = memory.attempts(self.project)[0]
                    self.assertIsNone(attempt['actual_model'])
                    self.assertIsNone(attempt['total_tokens'])
                    self.assertEqual(1, memory.retry_failed(self.project))
        self.assertEqual([], self.trace('turn/start'))
        configure(self.data, project_memory_profile=None)
        self.scenario(mode='project_memory')
        self.assertEqual('processed', self.process()['status'])

    def test_retry_uses_current_default_and_preserves_failed_measured_usage(self):
        self.capture()
        configure(self.data, processing_profile=profile())
        self.scenario(mode='provider_error', models=[{'id': 'picker', 'model': 'fixture-exact',
            'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}])
        failed = self.process()
        self.assertEqual('failed', failed['status'], failed)
        with Store(self.data) as store:
            ProjectMemory(store).retry_failed(self.project, include_quarantined=True)
        configure(self.data, processing_profile=None)
        self.scenario(mode='project_memory')
        success = self.process()
        self.assertEqual('processed', success['status'], success)
        self.assertEqual(failed['job_id'], success['job_id'])
        with Store(self.data) as store:
            attempts = list(reversed(ProjectMemory(store).attempts(self.project)))
            self.assertEqual(['fixture-exact', 'gpt-6-luna'], [a['requested_model'] for a in attempts])
            self.assertEqual(['fixture-exact', 'gpt-6-luna'], [a['actual_model'] for a in attempts])
            self.assertEqual(['failed', 'processed'], [a['outcome'] for a in attempts])
            self.assertEqual([12, 12], [a['total_tokens'] for a in attempts])
            self.assertEqual(['app_server_thread_total'] * 2, [a['usage_source'] for a in attempts])

    def test_active_profile_is_fixed_and_next_attempt_loads_new_configuration(self):
        configure(self.data, project_memory_profile=profile())
        self.capture()
        self.scenario(mode='project_memory', models=[{'id': 'picker', 'model': 'fixture-exact',
            'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}],
            config_change={'path': str(self.data / 'config.json'), 'values': {'project_memory_profile': None}})
        first = self.process()
        self.assertEqual('processed', first['status'], first)
        self.assertEqual('fixture-exact', first['execution_profile']['model'])
        self.capture('second')
        self.scenario(mode='project_memory')
        second = self.process()
        self.assertEqual('processed', second['status'], second)
        self.assertEqual(['fixture-exact', 'gpt-6-luna'], [r['model'] for r in self.trace('turn/start')])
        with Store(self.data) as store:
            self.assertEqual(2, len(ProjectMemory(store).snapshot(self.project)['episodes']))

    def test_source_deletion_during_profile_change_fences_commit_and_keeps_usage(self):
        configure(self.data, project_memory_profile=profile())
        self.capture()
        gate = self.root / 'release'
        self.scenario(mode='project_memory', gate=str(gate), models=[{'id': 'picker', 'model': 'fixture-exact',
            'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.process)
            try:
                deadline = time.monotonic() + 10
                while not gate.with_suffix('.ready').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(gate.with_suffix('.ready').exists())
                configure(self.data, project_memory_profile=None)
                with Store(self.data) as store:
                    source = store.timeline(self.project, limit=10)[0]
                    store.forget(self.project, ids=[source['id']])
            finally:
                gate.touch()
            result = future.result(timeout=10)
        self.assertEqual('deferred', result['status'], result)
        self.assertEqual('source_changed', result['code'])
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            self.assertEqual([], memory.snapshot(self.project)['episodes'])
            attempt = memory.attempts(self.project)[0]
            self.assertEqual('fixture-exact', attempt['actual_model'])
            self.assertEqual(12, attempt['total_tokens'])

    def test_retained_service_reloads_profile_after_failure(self):
        from codex_mem.service import run_service
        self.capture()
        configure(self.data, service_enabled=True, project_memory_profile=profile(timeout=1))
        self.scenario(mode='timeout',
            models=[{'id': 'picker', 'model': 'fixture-exact', 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}],
            config_change={'path': str(self.data / 'config.json'), 'values': {'project_memory_profile': None}},
            next_scenario={'mode': 'project_memory'})
        # Both queue readiness and retry use wall time; model deadlines stay monotonic.
        now = [time.time() + 40]
        def sleep(delay):
            now[0] += max(delay, 40)
        with mock.patch('time.time', side_effect=lambda: now[0]):
            result = run_service(self.data, clock=lambda: now[0], sleeper=sleep,
                                 poll_interval=1, max_cycles=4, processor_workers=1)
        self.assertEqual(['fixture-exact', 'gpt-6-luna'], [r['model'] for r in self.trace('turn/start')], result)
        with Store(self.data) as store:
            attempts = list(reversed(ProjectMemory(store).attempts(self.project)))
            self.assertEqual(['failed', 'processed'], [a['outcome'] for a in attempts])
            self.assertEqual(attempts[0]['job_id'], attempts[1]['job_id'])
            self.assertTrue(ProjectMemory(store).snapshot(self.project)['episodes'])

    def test_unknown_usage_stays_unknown_and_correction_keeps_profile(self):
        from codex_mem.processor import NativeProcessorRunner
        configure(self.data, project_memory_profile=profile())
        self.capture()
        reviewed = []
        def review(output, remaining):
            reviewed.append(remaining)
            return 'Correct the supplied packet.' if len(reviewed) == 1 else None
        self.scenario(mode='project_memory', omit_usage=True,
            models=[{'id': 'picker', 'model': 'fixture-exact', 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}],
            config_change={'path': str(self.data / 'config.json'), 'values': {'project_memory_profile': None}})
        # The documented candidate-review callback requests the adapter's one correction.
        # Its transport, packet checks and memory commit remain real.
        runner = NativeProcessorRunner(codex=str(self.executable), candidate_review=review)
        result = process_project_memory(self.project, self.data, runner=runner, timeout=10)
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(['fixture-exact'] * 2, [r['model'] for r in self.trace('turn/start')])
        self.assertEqual(['low'] * 2, [r['effort'] for r in self.trace('turn/start')])
        self.assertLess(reviewed[1], reviewed[0])
        with Store(self.data) as store:
            attempt = ProjectMemory(store).attempts(self.project)[0]
            self.assertIsNone(attempt['total_tokens'])
            self.assertEqual('unknown', attempt['usage_status'])
            self.assertEqual('turn-2', attempt['worker_turn_id'])
            self.assertEqual('fixture-exact', attempt['actual_model'])

    def split_task(self):
        for session, body in (('first', 'Task JOB-17: build the importer.'),
                              ('second', 'Continue task JOB-17. Local tests passed.')):
            with Store(self.data) as store:
                store.remember(self.project, 'Task', body, source='hook:UserPromptSubmit', session_id=session)
            self.scenario(mode='pm_task')
            result = self.process()
            self.assertEqual('processed', result['status'], result)
        with Store(self.data) as store:
            self.assertEqual(2, len(ProjectMemory(store).snapshot(self.project)['episodes']))

    def test_reconsideration_consolidation_uses_profile_and_does_not_replay(self):
        self.split_task()
        configure(self.data, project_memory_profile=profile())
        self.scenario(mode='pm_consolidate', models=[{'id': 'picker', 'model': 'fixture-exact',
            'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}])
        result = self.process()
        self.assertEqual('processed', result['status'], result)
        self.assertEqual('reconsideration', result['job_kind'])
        with Store(self.data) as store:
            state = ProjectMemory(store).snapshot(self.project)
            self.assertEqual(1, len(state['episodes']))
            self.assertEqual('fixture-exact', ProjectMemory(store).attempts(self.project)[0]['actual_model'])
            delivered = handle_hook(dict(hook_event_name='SessionStart', source='startup',
                cwd=str(self.project), session_id='reader'), store)
            self.assertIn('JOB-17', json.dumps(delivered))
        configure(self.data, project_memory_profile=None)
        self.assertEqual('idle', self.process()['status'])

    def test_concurrent_head_change_rejects_old_consolidation_atomically(self):
        self.split_task()
        configure(self.data, project_memory_profile=profile())
        gate = self.root / 'release'
        self.scenario(mode='pm_consolidate', gate=str(gate), models=[{'id': 'picker', 'model': 'fixture-exact',
            'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.process)
            try:
                deadline = time.monotonic() + 10
                while not gate.with_suffix('.ready').exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(gate.with_suffix('.ready').exists())
                configure(self.data, project_memory_profile=None)
                with Store(self.data) as store:
                    store.remember(self.project, 'Task', 'Continue task JOB-17. Correct the importer.',
                                   source='hook:UserPromptSubmit', session_id='third')
                self.scenario(mode='pm_continue')
                updated = self.process()
                self.assertEqual('processed', updated['status'], updated)
            finally:
                gate.touch()
            result = future.result(timeout=10)
        self.assertEqual('deferred', result['status'], result)
        self.assertEqual('source_changed', result['code'])
        with Store(self.data) as store:
            state = ProjectMemory(store).snapshot(self.project)
            self.assertEqual(2, len(state['episodes']))
            self.assertEqual([1, 2], sorted(episode['episode_version'] for episode in state['episodes']))
            attempts = ProjectMemory(store).attempts(self.project)
            rejected = next(a for a in attempts if a['requested_model'] == 'fixture-exact')
            self.assertEqual(12, rejected['total_tokens'])
            self.assertEqual('deferred', rejected['outcome'])

    def test_existing_configuration_queue_and_usage_migrate_without_rewriting_history(self):
        from pathlib import Path
        legacy = json.loads((Path(__file__).parent / 'fixtures/legacy_project_memory_profiles.json').read_text())
        self.project = legacy['project']
        with Store(self.data) as store:
            for name, table in legacy['tables'].items():
                if name == 'pm_usage_attempts':
                    store._connection.execute('DROP TABLE pm_usage_attempts')
                    store._connection.execute(table['schema'])
                for row in table['rows']:
                    store._connection.execute('INSERT INTO ' + name + '(' + ','.join(row) + ') VALUES ('
                        + ','.join('?' for _ in row) + ')', tuple(row.values()))
        (self.data / 'config.json').write_text(json.dumps(legacy['config']))
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            migrated = {a['attempt_id']: a for a in memory.attempts(self.project)}
            for old in legacy['tables']['pm_usage_attempts']['rows']:
                for key, value in old.items():
                    self.assertEqual(value, migrated[old['attempt_id']][key], key)
                self.assertEqual('legacy_requested_profile', migrated[old['attempt_id']]['profile_basis'])
                self.assertIsNone(migrated[old['attempt_id']]['actual_model'])
            memory.retry_failed(self.project)
        configure(self.data, project_memory_profile=profile())
        self.scenario(mode='project_memory', models=[{'id': 'picker', 'model': 'fixture-exact',
            'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}])
        first = self.process()
        self.assertEqual('processed', first['status'], first)
        # The revision commit legitimately invalidates the other pending snapshot.
        second = self.process()
        self.assertEqual('processed', second['status'], second)
        self.assertEqual('idle', self.process()['status'])
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            attempts = memory.attempts(self.project)
            self.assertEqual(5, len(attempts))
            by_id = {a['attempt_id']: a for a in attempts}
            for old in legacy['tables']['pm_usage_attempts']['rows']:
                self.assertEqual(old, {key: by_id[old['attempt_id']][key] for key in old})
            self.assertEqual(2, len(memory.snapshot(self.project)['episodes']))
            completed = next(row for row in legacy['tables']['pm_jobs']['rows'] if row['session_id'] == 'completed')
            self.assertNotIn(completed['id'], (first['job_id'], second['job_id']))

    def test_invalid_timeout_config_never_dispatches_and_explains_pending_work(self):
        self.capture()
        settings = json.loads((self.data / 'config.json').read_text())
        settings['project_memory_profile'] = profile(timeout=601)
        (self.data / 'config.json').write_text(json.dumps(settings))
        result = self.process()
        self.assertEqual('failed', result['status'], result)
        self.assertEqual('invalid_configuration', result['reason_code'])
        self.assertEqual([], self.trace('turn/start'))
        with Store(self.data) as store:
            self.assertGreater(ProjectMemory(store).snapshot(self.project)['coverage']['pending_events'], 0)
            self.assertEqual([], ProjectMemory(store).attempts(self.project))

    def test_attempt_id_cannot_be_reused_with_different_profile(self):
        from codex_mem.execution import ExecutionProfile
        self.capture()
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            claim = memory.claim(self.project)
            original = ExecutionProfile(**profile())
            attempt = memory.begin_attempt(claim, model=original.model, reasoning_effort='low', profile=original)
            for changed in (profile(timeout=20), {**profile(), 'provider': 'another'}, {**profile(), 'runtime': 'pi'}):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    memory.begin_attempt(claim, model=original.model, reasoning_effort='low',
                        attempt_id=attempt['attempt_id'], profile=ExecutionProfile(**changed))
            self.assertEqual(attempt, memory.attempts(self.project)[0])
