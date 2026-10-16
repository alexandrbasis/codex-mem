from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_mem.config import configure
from codex_mem.processor import MODEL, PROCESSOR_ID, REASONING_EFFORT
from codex_mem.service import SERVICE_STATE_FILENAME, enqueue, resume_storage_pending, run_service
from codex_mem.store import Store


class StorageResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = (self.root / 'project').resolve()
        self.project.mkdir()
        self.data = self.root / 'memory'
        self.now = 1000.0
        configure(self.data, capture_scope='selected', included_projects=[self.project], semantic_enabled=False)
        with Store(self.data):
            pass
        enqueue(self.project, self.data, clock=lambda: self.now)
        self.update(blocked=True, last_code='storage_failure', last_failure_job=None,
                    last_failure_detail={'stage': 'storage', 'code': 'storage_failure',
                                         'operation': 'claim', 'reason_code': 'unknown', 'retry_safe': False})

    def state(self):
        return json.loads((self.data / SERVICE_STATE_FILENAME).read_text())

    def record(self):
        return self.state()['projects'][str(self.project)]

    def update(self, **values):
        state = self.state()
        state['projects'][str(self.project)].update(values)
        (self.data / SERVICE_STATE_FILENAME).write_text(json.dumps(state))

    def resume(self):
        return resume_storage_pending(self.project, self.data, clock=lambda: self.now)

    def seed(self, session='session'):
        with Store(self.data) as store:
            store.remember(self.project, 'Tool', 'Verified project evidence.',
                           source='hook:PostToolUse', session_id=session, turn_id='turn')

    def test_resumes_fresh_work_without_retrying_failed_or_quarantined_jobs(self):
        jobs = []
        for session, code in [('failed', 'runner_failure'), ('quarantine', 'invalid_response')]:
            self.seed(session)
            with Store(self.data) as store:
                job = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
                store.fail_observation_batch(self.project, job['job_id'], job['lease_token'], code=code)
                jobs.append(job['job_id'])
        self.seed('fresh')
        self.assertEqual({'status': 'queued', 'project': str(self.project), 'retry_failed': False}, self.resume())
        calls = []
        def processor(project, **kwargs):
            calls.append(kwargs)
            with Store(self.data) as store:
                job = store.claim_observation_batch(project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
                                                     retry_failed=kwargs['retry_failed'])
                self.assertEqual('fresh', job['sources'][0]['session_id'])
                store.finish_observation_batch(project, job['job_id'], job['lease_token'], disposition='skipped')
            return {'status': 'skipped'}
        run_service(self.data, processor=processor, processor_workers=1, clock=lambda: self.now,
                    sleeper=lambda _: None, max_cycles=1)
        self.assertFalse(calls[0]['retry_failed'])
        with Store(self.data) as store:
            for job_id in jobs:
                job = store.observation_job_status(self.project, job_id)
                self.assertEqual(('failed', 1), (job['status'], job['attempt_count']))

    def test_rejects_other_blockers_postclaim_metadata_and_existing_job(self):
        for changes in ({'last_code': 'runner_failure'}, {'last_failure_job': 'exact-job'},
                        {'last_failure_detail': None}, {'retry_requested': True, 'retry_generation': 1},
                        {'last_failure_detail': {'stage': 'storage', 'code': 'storage_failure', 'operation': 'finalize'}}):
            before = self.state()
            self.update(**changes)
            changed = self.state()
            self.assertEqual('blocked', self.resume()['status'])
            self.assertEqual(changed, self.state())
            (self.data / SERVICE_STATE_FILENAME).write_text(json.dumps(before))

    def test_rejects_inflight_work(self):
        self.update(inflight_generation=1, inflight_until=2000)
        self.assertEqual('work_inflight', self.resume()['code'])

    def test_rejects_running_durable_lease(self):
        self.seed()
        with Store(self.data) as store:
            store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
        before = self.state()
        self.assertEqual('lease_unavailable', self.resume()['code'])
        self.assertEqual(before, self.state())

    def test_missing_database_fails_closed(self):
        (self.data / 'memory.sqlite3').unlink()
        self.assertEqual('lease_unavailable', self.resume()['code'])

    def test_preserves_exact_target_attempt_and_cooldown(self):
        self.seed('old')
        with Store(self.data) as store:
            job = store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
            store.fail_observation_batch(self.project, job['job_id'], job['lease_token'], code='runner_failure')
        self.update(attempts=2, retry_job_id=job['job_id'], retry_error_code='runner_failure',
                    retry_attempt_count=1, due_at=4600)
        before = self.record()
        self.assertEqual('queued', self.resume()['status'])
        after = self.record()
        for field in ('attempts', 'retry_job_id', 'retry_error_code', 'retry_attempt_count', 'due_at', 'pending_retries'):
            self.assertEqual(before[field], after[field])
        self.assertTrue(after['fresh_work_due'])
