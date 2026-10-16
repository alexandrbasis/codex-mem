from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_mem.config import configure
from codex_mem.processor import MODEL, PROCESSOR_ID, REASONING_EFFORT
from codex_mem.service import SERVICE_STATE_FILENAME, enqueue, recover_runner_failure, run_service
from codex_mem.store import Store


class RetryFairnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = (self.root / 'project').resolve()
        self.project.mkdir()
        self.data = self.root / 'memory'
        self.now = 1000.0
        configure(self.data, capture_scope='selected', included_projects=[self.project], semantic_enabled=False)
        self.jobs = []
        for session in ('old-a', 'old-b'):
            self.seed(session)
            with Store(self.data) as store:
                job = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
                store.fail_observation_batch(self.project, job['job_id'], job['lease_token'],
                                             code='runner_failure', reason_code='jev_filter_transport')
                self.jobs.append(job['job_id'])
        enqueue(self.project, self.data, clock=lambda: self.now)
        state = self.state()
        record = state['projects'][str(self.project)]
        record.update(attempts=3, due_at=4600.0, retry_job_id=self.jobs[0],
                      retry_error_code='runner_failure', retry_attempt_count=1,
                      pending_retries=[dict(job_id=self.jobs[1], error_code='runner_failure',
                                            attempt_count=1, attempts=3, due_at=4700.0)])
        record.pop("fresh_work_due", None)  # Upgrade from the previous queue schema.
        self.write(state)
        self.calls = []

    def seed(self, session):
        with Store(self.data) as store:
            store.remember(self.project, 'Source', 'Verified project evidence.',
                           source='hook:PostToolUse', session_id=session)

    def state(self):
        return json.loads((self.data / SERVICE_STATE_FILENAME).read_text())

    def write(self, state):
        (self.data / SERVICE_STATE_FILENAME).write_text(json.dumps(state))

    def processor(self, project, **kw):
        self.calls.append((self.now, dict(kw)))
        with Store(self.data) as store:
            job = store.claim_observation_batch(project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                retry_failed=kw['retry_failed'], retry_job_id=kw.get('retry_job_id'),
                retry_error_code=kw.get('retry_error_code'), retry_attempt_count=kw.get('retry_attempt_count'),
                parallel_sessions=kw.get('parallel_sessions', False))
            if job is None:
                return {'status': 'idle'}
            store.finish_observation_batch(project, job['job_id'], job['lease_token'], disposition='skipped')
            return {'status': 'skipped', 'job_id': job['job_id']}

    def run_worker(self, cycles=1):
        return run_service(self.data, processor=self.processor, processor_workers=1,
                           clock=lambda: self.now, sleeper=lambda _: None, max_cycles=cycles)

    def test_legacy_cooldowns_allow_fresh_work_and_preserve_exact_retry_after_restart(self):
        self.seed('new-session')
        self.run_worker()
        self.assertEqual(1, len(self.calls))
        self.assertIsNone(self.calls[0][1].get('retry_job_id'))
        self.assertFalse(self.calls[0][1]['retry_failed'])
        record = self.state()['projects'][str(self.project)]
        self.assertEqual((self.jobs[0], 4600.0, 1),
                         (record['retry_job_id'], record['due_at'], record['retry_attempt_count']))
        self.assertEqual(self.jobs[1], record['pending_retries'][0]['job_id'])
        self.run_worker(cycles=4)
        # The metadata hint avoids even idle processor calls during cooldown.
        self.assertEqual(1, len(self.calls))
        self.run_worker(cycles=4)
        self.assertEqual(1, len(self.calls))
        self.now = 4600.0
        self.run_worker()
        self.assertEqual(self.jobs[0], self.calls[-1][1]['retry_job_id'])
        self.assertEqual(('runner_failure', 1),
                         (self.calls[-1][1]['retry_error_code'], self.calls[-1][1]['retry_attempt_count']))
        with Store(self.data) as store:
            first = store.observation_job_status(self.project, self.jobs[0])
            second = store.observation_job_status(self.project, self.jobs[1])
        self.assertEqual(('skipped', 2), (first['status'], first['attempt_count']))
        self.assertEqual(('failed', 1), (second['status'], second['attempt_count']))

    def test_new_capture_wakes_fresh_pass_without_accelerating_retry(self):
        self.run_worker(cycles=3)
        count = len(self.calls)
        self.seed('old-a')  # Failed sources are excluded; the session has no running lease.
        enqueue(self.project, self.data, clock=lambda: self.now)
        self.run_worker()
        self.assertEqual(count + 1, len(self.calls))
        self.assertIsNone(self.calls[-1][1].get('retry_job_id'))
        self.assertEqual(4600.0, self.state()['projects'][str(self.project)]['due_at'])
        with Store(self.data) as store:
            self.assertEqual(1, store.observation_job_status(self.project, self.jobs[0])['attempt_count'])

    def test_capture_coalescing_preserves_exact_retry_cooldowns(self):
        self.seed('new-session')
        enqueue(self.project, self.data, clock=lambda: self.now, coalesce_seconds=5)
        self.run_worker()
        self.assertIsNone(self.calls[-1][1].get('retry_job_id'))
        record = self.state()['projects'][str(self.project)]
        self.assertEqual((self.jobs[0], 4600.0, 1),
                         (record['retry_job_id'], record['due_at'], record['retry_attempt_count']))
        self.assertEqual(self.jobs[1], record['pending_retries'][0]['job_id'])
        with Store(self.data) as store:
            self.assertEqual(1, store.observation_job_status(self.project, self.jobs[0])['attempt_count'])
            self.assertEqual(1, store.observation_job_status(self.project, self.jobs[1])['attempt_count'])

    def test_running_same_session_is_not_bypassed(self):
        self.seed('occupied-session')
        with Store(self.data) as store:
            running = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                                                    parallel_sessions=True)
        self.seed('occupied-session')
        self.run_worker(cycles=3)
        self.assertEqual([], self.calls)
        with Store(self.data) as store:
            self.assertEqual('running', store.observation_job_status(self.project, running['job_id'])['status'])
        self.assertEqual(4600.0, self.state()['projects'][str(self.project)]['due_at'])

    def test_fresh_failures_reserve_capacity_for_all_exact_permissions(self):
        for session in ('fresh-a', 'fresh-b', 'fresh-c'):
            self.seed(session)
        calls = []
        def fail_fresh(project, **kw):
            calls.append(dict(kw))
            with Store(self.data) as store:
                job = store.claim_observation_batch(project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                    parallel_sessions=kw.get('parallel_sessions', False))
                self.assertIsNotNone(job)
                store.fail_observation_batch(project, job['job_id'], job['lease_token'],
                                             code='runner_failure', reason_code='jev_filter_transport')
                return dict(status='failed', code='runner_failure', reason_code='jev_filter_transport',
                            job_id=job['job_id'])
        run_service(self.data, processor=fail_fresh, processor_workers=4,
                    clock=lambda: self.now, sleeper=lambda _: None, max_cycles=1)
        self.assertEqual(2, len(calls))  # Two existing targets reserve two of the four slots.
        self.assertTrue(all(call.get('retry_job_id') is None for call in calls))
        record = self.state()['projects'][str(self.project)]
        targets = [dict(job_id=record['retry_job_id'], due_at=record['due_at'])] + record['pending_retries']
        self.assertEqual(4, len(targets))
        self.assertEqual(4600.0, next(target['due_at'] for target in targets if target['job_id'] == self.jobs[0]))
        self.assertEqual(4700.0, next(target['due_at'] for target in targets if target['job_id'] == self.jobs[1]))
        self.run_worker(cycles=3)
        self.assertEqual([], self.calls)  # Full permission queue waits without dropping any target.

    def saturate_retry_targets(self):
        state = self.state()
        record = state['projects'][str(self.project)]
        for session in ('old-c', 'old-d'):
            self.seed(session)
            with Store(self.data) as store:
                job = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
                store.fail_observation_batch(self.project, job['job_id'], job['lease_token'],
                                             code='runner_failure', reason_code='jev_filter_transport')
            self.jobs.append(job['job_id'])
            record['pending_retries'].append(dict(job_id=job['job_id'], error_code='runner_failure',
                                                  attempt_count=1, attempts=3, due_at=4800.0))
        record.update(last_code='runner_failure', fresh_work_due=True)
        self.write(state)
        self.seed('rejected-session')
        with Store(self.data) as store:
            job = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            store.fail_observation_batch(self.project, job['job_id'], job['lease_token'],
                                         code='invalid_response', reason_code='jev_quality_uncertain')
        self.rejected_job = job['job_id']
        self.seed('new-session')

    def test_explicit_active_runner_recovery_frees_saturated_capacity_for_fresh_work(self):
        self.saturate_retry_targets()
        before = self.state()['projects'][str(self.project)]['pending_retries']
        result = recover_runner_failure(self.project, self.data, job_id=self.jobs[0], clock=lambda: self.now)
        self.assertEqual('queued', result['status'])
        record = self.state()['projects'][str(self.project)]
        self.assertEqual(before, record['pending_retries'])
        self.assertEqual((self.jobs[0], 'runner_failure', 1, self.now),
                         (record['retry_job_id'], record['retry_error_code'], record['retry_attempt_count'], record['due_at']))
        self.run_worker(cycles=2)
        self.assertEqual(self.jobs[0], self.calls[0][1]['retry_job_id'])
        self.assertIsNone(self.calls[1][1].get('retry_job_id'))
        self.assertTrue(all(not call[1]['retry_failed'] for call in self.calls))
        with Store(self.data) as store:
            self.assertEqual('skipped', store.observation_job_status(self.project, self.jobs[0])['status'])
            for job_id in self.jobs[1:] + [self.rejected_job]:
                self.assertEqual(('failed', 1), tuple(store.observation_job_status(self.project, job_id)[k]
                                                   for k in ('status', 'attempt_count')))

    def test_explicit_pending_runner_recovery_swaps_one_exact_permission(self):
        self.saturate_retry_targets()
        original = self.state()['projects'][str(self.project)]
        previous_active = {k: original[k] for k in ('retry_job_id', 'retry_error_code', 'retry_attempt_count', 'attempts', 'due_at')}
        self.assertEqual('queued', recover_runner_failure(self.project, self.data,
                         job_id=self.jobs[2], clock=lambda: self.now)['status'])
        record = self.state()['projects'][str(self.project)]
        self.assertEqual(self.jobs[2], record['retry_job_id'])
        self.assertEqual(3, len(record['pending_retries']))
        preserved = next(target for target in record['pending_retries'] if target['job_id'] == self.jobs[0])
        self.assertEqual({'job_id': previous_active['retry_job_id'], 'error_code': previous_active['retry_error_code'],
                          'attempt_count': previous_active['retry_attempt_count'], 'attempts': previous_active['attempts'],
                          'due_at': previous_active['due_at']}, preserved)
        self.run_worker(cycles=1)
        self.assertEqual(self.jobs[2], self.calls[0][1]['retry_job_id'])
        with Store(self.data) as store:
            self.assertEqual(('failed', 1), tuple(store.observation_job_status(self.project, self.jobs[0])[k]
                                               for k in ('status', 'attempt_count')))

    def test_remaining_scheduled_runner_retries_can_be_recovered_after_success(self):
        self.saturate_retry_targets()
        for job_id in self.jobs:
            self.assertEqual('queued', recover_runner_failure(self.project, self.data,
                             job_id=job_id, clock=lambda: self.now)['status'])
            self.run_worker(cycles=1)
        self.assertEqual(self.jobs, [call[1]['retry_job_id'] for call in self.calls])
        self.run_worker(cycles=1)
        self.assertIsNone(self.calls[-1][1].get('retry_job_id'))
        with Store(self.data) as store:
            self.assertEqual(('failed', 1), tuple(store.observation_job_status(self.project, self.rejected_job)[k]
                                               for k in ('status', 'attempt_count')))

    def test_explicit_active_runner_recovery_failure_preserves_saturated_permissions(self):
        self.saturate_retry_targets()
        self.assertEqual('queued', recover_runner_failure(self.project, self.data,
                         job_id=self.jobs[0], clock=lambda: self.now)['status'])
        def fail_exact(project, **kw):
            self.assertEqual(self.jobs[0], kw['retry_job_id'])
            self.assertFalse(kw['retry_failed'])
            with Store(self.data) as store:
                job = store.claim_observation_batch(project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                    retry_job_id=kw['retry_job_id'], retry_error_code=kw['retry_error_code'],
                    retry_attempt_count=kw['retry_attempt_count'])
                store.fail_observation_batch(project, job['job_id'], job['lease_token'],
                                             code='runner_failure', reason_code='jev_filter_transport')
            return dict(status='failed', code='runner_failure', reason_code='jev_filter_transport', job_id=job['job_id'])
        run_service(self.data, processor=fail_exact, processor_workers=2,
                    clock=lambda: self.now, sleeper=lambda _: None, max_cycles=1)
        record = self.state()['projects'][str(self.project)]
        ids = [record['retry_job_id']] + [target['job_id'] for target in record['pending_retries']]
        self.assertCountEqual(self.jobs, ids)
        self.assertEqual(4, len(ids))
        self.assertFalse(record['blocked'])

    def test_active_runner_recovery_rejects_mismatched_and_inflight_targets(self):
        self.saturate_retry_targets()
        original = self.state()
        cases = [('foreign-target', None), ('unscheduled', None), ('attempt', {'retry_attempt_count': 99}),
                 ('parked', {'parked': 'rejected_batches'}), ('other-blocker', {'last_code': 'model_mismatch'}),
                 ('code', {'retry_error_code': 'protocol_error'}),
                 ('inflight', {'inflight_generation': original['projects'][str(self.project)]['generation'],
                               'inflight_until': 1500.0})]
        for name, changes in cases:
            with self.subTest(name=name):
                state = self.state()
                if changes:
                    state['projects'][str(self.project)].update(changes)
                    self.write(state)
                before = (self.data / SERVICE_STATE_FILENAME).read_bytes()
                job_id = self.rejected_job if name == 'foreign-target' else ('a' * 32 if name == 'unscheduled' else self.jobs[0])
                result = recover_runner_failure(self.project, self.data, job_id=job_id, clock=lambda: self.now)
                self.assertEqual('blocked', result['status'])
                self.assertEqual(before, (self.data / SERVICE_STATE_FILENAME).read_bytes())
                self.write(original)

    def test_permanent_block_does_not_run_fresh_work(self):
        state = self.state()
        state['projects'][str(self.project)].update(blocked=True, last_code='model_mismatch')
        self.write(state)
        self.seed('new-session')
        self.run_worker(cycles=3)
        self.assertEqual([], self.calls)


if __name__ == '__main__':
    unittest.main()
