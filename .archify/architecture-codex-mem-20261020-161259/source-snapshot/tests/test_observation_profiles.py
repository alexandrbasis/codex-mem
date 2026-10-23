"""Observation profiles through host capture, real queue and external runtime."""
import json
import hashlib
import unittest
from pathlib import Path
import sqlite3
import os
import time
from unittest import mock
from concurrent.futures import ThreadPoolExecutor

import test_execution_contract as fixture
from codex_mem.config import configure
from codex_mem.hooks import handle_hook
from codex_mem.processor import process_pending
from codex_mem.store import Store


def profile(model='fixture-exact', effort='low', timeout=10):
    return dict(runtime='codex', provider='openai', model=model,
                reasoning_effort=effort, timeout=timeout)


class ObservationProfileTests(unittest.TestCase):
    scenario = fixture.ExecutionContractTests.scenario

    def setUp(self):
        fixture.ExecutionContractTests.setUp(self)
        self.data = self.root / 'memory'
        self.project = self.root / 'project'
        self.project.mkdir()
        configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False)

    def capture(self, session='session'):
        with Store(self.data) as store:
            return handle_hook(dict(hook_event_name='UserPromptSubmit', cwd=str(self.project),
                session_id=session, turn_id='turn', prompt='Keep local import.'), store)

    def process(self, **options):
        return process_pending(self.project, self.data, codex=str(self.executable), timeout=10, **options)

    def trace(self, method):
        return [row['params'] for row in map(json.loads, self.executable.with_suffix('.trace').read_text().splitlines())
                if row.get('method') == method]

    def test_override_selects_exact_available_profile_and_success_does_not_replay(self):
        configure(self.data, processing_profile=profile('missing-default'), observation_profile=profile())
        self.scenario(models=[{'id': 'fixture-exact', 'model': 'fixture-exact', 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}])
        self.capture()
        result = self.process()
        self.assertEqual('skipped', result['status'], result)
        self.assertEqual('fixture-exact', self.trace('thread/start')[0]['model'])
        self.assertEqual('low', self.trace('turn/start')[0]['effort'])
        configure(self.data, observation_profile=None, processing_profile=None)
        self.scenario()
        self.assertEqual('idle', self.process()['status'])
        self.assertEqual(1, len(self.trace('turn/start')))

    def test_maximum_profile_model_identifier_reaches_native_validation(self):
        model = 'm' * 256
        configure(self.data, observation_profile=profile(model))
        self.scenario(models=[{'id': 'picker', 'model': model,
            'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}])
        self.capture()
        result = self.process()
        self.assertEqual('skipped', result['status'], result)
        self.assertEqual(model, self.trace('turn/start')[0]['model'])

    def test_failed_attempt_retry_keeps_job_and_each_profile_and_measured_usage(self):
        configure(self.data, observation_profile=profile())
        self.scenario(mode='provider_error', models=[{'id': 'fixture-exact', 'model': 'fixture-exact',
            'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}])
        self.capture()
        failed = self.process()
        self.assertEqual('failed', failed['status'], failed)
        configure(self.data, observation_profile=None)
        self.scenario()
        retried = self.process(retry_failed=True)
        self.assertEqual('skipped', retried['status'], retried)
        self.assertEqual(failed['job_id'], retried['job_id'])
        with Store(self.data) as store:
            attempts = store.observation_attempts(self.project, failed['job_id'])
            self.assertEqual(['fixture-exact', 'gpt-6-luna'], [a['requested_model'] for a in attempts])
            self.assertEqual(['fixture-exact', 'gpt-6-luna'], [a['actual_model'] for a in attempts])
            self.assertEqual(['failed', 'skipped'], [a['outcome'] for a in attempts])
            self.assertEqual([12, 12], [a['total_tokens'] for a in attempts])
            self.assertEqual(['app_server_thread_total'] * 2, [a['usage_source'] for a in attempts])
            self.assertEqual(['codex'] * 2, [a['runtime'] for a in attempts])
            self.assertEqual(['openai'] * 2, [a['provider'] for a in attempts])
            self.assertEqual(['fixture-worker'] * 2, [a['worker_thread_id'] for a in attempts])
            self.assertEqual(['low', 'medium'], [a['reasoning_effort'] for a in attempts])
            groups = store.status(self.project)['observer_usage']['models']
            self.assertEqual(['fixture-exact', 'gpt-6-luna'], sorted(g['model'] for g in groups))
            self.assertEqual([12, 12], [(g['totals']['reported'] or g['totals']['partial'])['total_tokens'] for g in groups])
        from codex_mem.cost_report import build_cost_report
        report = build_cost_report(self.data, project=self.project)
        self.assertEqual(['fixture-exact', 'gpt-6-luna'], sorted(g['value'] for g in report['groups']['observer']['model']['rows']))
        self.assertEqual('idle', self.process()['status'])

    def test_existing_queue_and_accounting_migrate_without_new_jobs_or_rewritten_history(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/legacy_observation_profiles.json').read_text())
        self.project = fixture['project']
        with Store(self.data) as store:
            # Restore rows/schema produced by the exact pre-#5 implementation.
            for name, table in fixture['tables'].items():
                if name == 'observer_usage_attempts':
                    store._connection.execute(table['schema'])
                for row in table['rows']:
                    store._connection.execute('INSERT INTO ' + name + '(' + ','.join(row) + ') VALUES ('
                        + ','.join('?' for _ in row) + ')', tuple(row.values()))
        (self.data / 'config.json').write_text(json.dumps(fixture['config']))
        original = {r['session_id']: r for r in fixture['tables']['observation_jobs']['rows']}
        recovered = self.process()
        self.assertEqual('skipped', recovered['status'], recovered)
        self.assertEqual(original['running']['id'], recovered['job_id'])
        retried = self.process(retry_failed=True)
        self.assertEqual('skipped', retried['status'], retried)
        self.assertEqual(original['failed']['id'], retried['job_id'])
        self.assertEqual('idle', self.process()['status'])
        with Store(self.data) as store:
            for session, old in original.items():
                job = store.observation_job_status(self.project, old['id'])
                self.assertEqual(old['model'], job['model'])
                attempts = store.observation_attempts(self.project, old['id'])
                self.assertEqual('gpt-5.6-luna', attempts[0]['requested_model'])
                self.assertEqual('legacy_requested_job_profile', attempts[0]['profile_basis'])
                self.assertIsNone(attempts[0]['actual_model'])
                if session != 'running':
                    self.assertEqual(12, attempts[0]['total_tokens'])
                    self.assertEqual('historic-' + session, attempts[0]['worker_thread_id'])
                if session == 'skipped':
                    self.assertEqual(1, len(attempts))
                else:
                    self.assertEqual('gpt-6-luna', attempts[1]['requested_model'])
                    self.assertEqual('gpt-6-luna', attempts[1]['actual_model'])
            self.assertEqual(3, store._connection.execute('SELECT count(*) FROM observation_jobs').fetchone()[0])

    def test_legacy_failed_duplicate_cannot_replay_sources_completed_by_another_profile(self):
        old = json.loads((Path(__file__).parent / 'fixtures/legacy_observation_profiles.json').read_text())
        self.project = old['project']
        jobs = old['tables']['observation_jobs']['rows']
        failed = next(row for row in jobs if row['status'] == 'failed')
        successful = next(row for row in jobs if row['status'] == 'skipped')
        links = old['tables']['observation_job_sources']['rows']
        completed_source = next(row['source_id'] for row in links if row['job_id'] == successful['id'])
        # A previous profile could create a second job for an already-linked source.
        failed['input_fingerprint'] = hashlib.sha256('\x00'.join((
            self.project, failed['processor_id'], failed['model'],
            failed['reasoning_effort'], completed_source)).encode()).hexdigest()
        for row in links:
            if row['job_id'] == failed['id']:
                row['source_id'] = completed_source
        for row in jobs:
            if row['status'] == 'running':
                row['status'] = 'skipped'
        with Store(self.data) as store:
            for name, table in old['tables'].items():
                if name == 'observer_usage_attempts':
                    store._connection.execute(table['schema'])
                for row in table['rows']:
                    store._connection.execute('INSERT INTO ' + name + '(' + ','.join(row) + ') VALUES ('
                        + ','.join('?' for _ in row) + ')', tuple(row.values()))
            # Remove the now-unlinked source to keep only the historical duplicate pending.
            store._connection.execute('DELETE FROM entries WHERE id NOT IN (SELECT source_id FROM observation_job_sources)')
        (self.data / 'config.json').write_text(json.dumps(old['config']))
        result = self.process(retry_failed=True)
        self.assertEqual('idle', result['status'], result)
        self.assertFalse(self.executable.with_suffix('.trace').exists())
        with Store(self.data) as store:
            self.assertEqual(1, store.observation_job_status(self.project, failed['id'])['attempt_count'])

    def test_explicit_other_processor_retry_is_distinct_from_profile_retry_and_success(self):
        with Store(self.data) as store:
            store.remember(self.project, 'Raw', 'Preserve local source.', source='hook:PostToolUse')
            original = store.claim_observation_batch(self.project, 'processor-a', 'model-a', 'low')
            store.fail_observation_batch(self.project, original['job_id'], original['lease_token'], 'invalid_response')
            self.assertIsNone(store.claim_observation_batch(self.project, 'processor-b', 'model-b', 'medium'))
            same = store.claim_observation_batch(self.project, 'processor-a', 'model-b', 'medium', retry_failed=True)
            self.assertEqual(original['job_id'], same['job_id'])
            store.fail_observation_batch(self.project, same['job_id'], same['lease_token'], 'invalid_response')
            other = store.claim_observation_batch(self.project, 'processor-b', 'model-b', 'medium', retry_failed=True)
            self.assertIsNotNone(other)
            self.assertNotEqual(original['job_id'], other['job_id'])
            self.assertEqual([row['id'] for row in original['sources']], [row['id'] for row in other['sources']])
            store.finish_observation_batch(self.project, other['job_id'], other['lease_token'], disposition='skipped')
            for processor in ('processor-a', 'processor-b', 'processor-c'):
                self.assertIsNone(store.claim_observation_batch(self.project, processor, 'model-c', 'high', retry_failed=True))

    def test_other_processor_cannot_bypass_one_shot_or_recovery_parent(self):
        for successor in (False, True):
            with self.subTest(successor=successor), Store(self.root / str(successor)) as store:
                store.remember(self.project, 'Raw', 'Keep exact maintenance permission.', source='hook:PostToolUse')
                parent = store.claim_observation_batch(self.project, 'processor-a', 'model-a', 'low')
                store.fail_observation_batch(self.project, parent['job_id'], parent['lease_token'], 'invalid_response')
                one_shot = store.claim_observation_batch(self.project, 'processor-a', 'model-a', 'low',
                    retry_job_id=parent['job_id'], retry_error_code='invalid_response',
                    retry_attempt_count=1, retry_input_fingerprint=parent['input_fingerprint'],
                    retry_previous_profile=successor, retry_one_shot=True, lease_seconds=1)
                self.assertIsNotNone(one_shot)
                self.assertIsNone(store.claim_observation_batch(self.project, 'processor-b', 'model-b', 'low', retry_failed=True))
                time.sleep(1.05)
                self.assertIsNone(store.claim_observation_batch(self.project, 'processor-b', 'model-b', 'low', retry_failed=True))
                self.assertEqual('recovery_exhausted', store.observation_job_status(self.project, one_shot['job_id'])['disposition'])
                self.assertIsNone(store.claim_observation_batch(self.project, 'processor-a', 'model-b', 'low', retry_failed=True))

    def test_invalid_profile_preserves_pending_work_and_explains_missing_capability(self):
        self.capture()
        for selected, reason in ((profile('not-listed'), 'exact_model_unavailable'),
                                 (profile('gpt-6-luna', 'high'), 'unsupported_reasoning'),
                                 ({**profile(), 'provider': 'unverified'}, 'unsupported_provider')):
            with self.subTest(reason=reason):
                configure(self.data, observation_profile=selected)
                failed = self.process(retry_failed=True)
                self.assertEqual('failed', failed['status'], failed)
                self.assertEqual(reason, failed['reason_code'])
                with Store(self.data) as store:
                    attempts = store.observation_attempts(self.project, failed['job_id'])
                    self.assertIsNone(attempts[-1]['actual_model'])
                    self.assertIsNone(attempts[-1]['total_tokens'])
        self.assertEqual([], self.trace('turn/start'))
        configure(self.data, observation_profile=None)
        self.assertEqual('skipped', self.process(retry_failed=True)['status'])

    def test_running_service_reloads_profile_for_automatic_retry(self):
        from codex_mem.service import enqueue, run_service
        configure(self.data, service_enabled=True, observation_profile=profile())
        self.capture()
        self.scenario(mode='provider_error', error_info='internalServerError',
            models=[{'id': 'fixture-exact', 'model': 'fixture-exact', 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}],
            config_change={'path': str(self.data / 'config.json'), 'values': {'observation_profile': None}},
            next_scenario={})
        # The service keeps its normal processor; only executable discovery selects the fixture.
        (self.root / 'codex').symlink_to(self.executable)
        now = [time.time()]
        enqueue(self.project, self.data, clock=lambda: now[0])
        def sleep(delay):
            now[0] += delay
        with mock.patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH']):
            result = run_service(self.data, clock=lambda: now[0], sleeper=sleep,
                                 poll_interval=1, retry_backoff=1, max_cycles=5, processor_workers=1)
        self.assertEqual(['fixture-exact', 'gpt-6-luna'], [r['model'] for r in self.trace('turn/start')], result)
        with Store(self.data) as store:
            jobs = store.status(self.project)['observation_jobs']['recent']
            self.assertEqual(1, len(jobs))
            attempts = store.observation_attempts(self.project, jobs[0]['job_id'])
            self.assertEqual(['fixture-exact', 'gpt-6-luna'], [a['requested_model'] for a in attempts])
            self.assertEqual(['failed', 'skipped'], [a['outcome'] for a in attempts])

    def test_active_profile_is_immutable_and_source_deletion_cancels_commit(self):
        configure(self.data, observation_profile=profile())
        self.capture()
        gate = self.root / 'release'
        self.scenario(gate=str(gate), models=[{'id': 'fixture-exact', 'model': 'fixture-exact',
            'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.process)
            deadline = time.monotonic() + 10
            while not gate.with_suffix('.ready').exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(gate.with_suffix('.ready').exists())
            configure(self.data, observation_profile=None)
            with Store(self.data) as store:
                source = store.timeline(self.project, limit=10)[0]
                store.forget(self.project, ids=[source['id']])
            gate.touch()
            result = future.result(timeout=10)
        self.assertEqual('failed', result['status'], result)
        self.assertEqual('fixture-exact', result['execution_profile']['model'])
        self.assertEqual('fixture-exact', self.trace('turn/start')[0]['model'])
        with Store(self.data) as store:
            self.assertEqual([], store.search(self.project, 'local import'))
            attempts = store.observation_attempts(self.project, result['job_id'])
            self.assertEqual(12, attempts[0]['total_tokens'])
            self.assertEqual('fixture-exact', attempts[0]['actual_model'])

    def test_summary_correction_keeps_profile_when_configuration_changes(self):
        from tests.test_jev_quality import response
        configure(self.data, observation_profile=profile(), jev_quality_enabled=True)
        with Store(self.data) as store:
            handle_hook(dict(hook_event_name='Stop', cwd=str(self.project), session_id='summary',
                             turn_id='summary-turn', last_assistant_message='Keep local import.'), store)
        summary = dict(title='Import choice', request=None, investigated='', learned='Keep local import.',
                       completed='', next_steps='', notes='', source_ids=['s1'])
        self.scenario(models=[{'id': 'fixture-exact', 'model': 'fixture-exact', 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]}],
            config_change={'path': str(self.data / 'config.json'), 'values': {'observation_profile': None}},
            outputs=[{'result': dict(notes=[], disposition='processed', session_summary=summary)},
                     {'result': dict(notes=[], disposition='processed', session_summary={**summary,
                         'learned': 'The assistant reported: Keep local import.'})}])
        def evaluator(payload):
            # Substitute only the optional external quality response. The real
            # gate, correction, source guards and atomic summary commit all run.
            return response(payload, .5, .1) if len(self.trace('turn/start')) == 1 else response(payload)
        result = self.process(jev_quality_evaluator=evaluator)
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(1, result['session_summary_count'])
        self.assertEqual(['fixture-exact', 'fixture-exact'], [r['model'] for r in self.trace('turn/start')])
        self.assertEqual(['low', 'low'], [r['effort'] for r in self.trace('turn/start')])
        with Store(self.data) as store:
            attempt = store.observation_attempts(self.project, result['job_id'])[0]
            self.assertEqual('fixture-exact', attempt['requested_model'])
            self.assertEqual('turn-2', attempt['worker_turn_id'])
            self.assertEqual(24, attempt['total_tokens'])
            self.assertIn('Keep local import.', store.context(self.project))

    def test_claim_persists_profile_before_worker_can_crash(self):
        from codex_mem.processor import PROCESSOR_ID
        self.capture()
        with Store(self.data) as store:
            claim = store.claim_observation_batch(self.project, PROCESSOR_ID, 'fixture-exact', 'low',
                                                 execution_profile=profile())
            attempt = store.observation_attempts(self.project, claim['job_id'])[0]
            self.assertEqual('running', attempt['outcome'])
            self.assertEqual('fixture-exact', attempt['requested_model'])
            self.assertEqual(10, attempt['timeout'])
            self.assertIsNone(attempt['actual_model'])
