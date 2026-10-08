"""Explicit profile recovery preserves quarantine history and source boundaries."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from codex_mem.observer_usage_store import begin_attempt
from codex_mem.store import Store, StoreError, _legacy_observation_fingerprint, _observation_fingerprint, project_key


class LegacyQuarantineRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.store = Store(self.root / 'data')
        self.processor = 'codex-mem-native-observation-v1'
        self.source = self.store.remember(
            self.project, 'Captured source', 'A verified requirement remains unprocessed.',
            source='hook:Stop', session_id='legacy-session',
        )
        self.parent = self.store.claim_observation_batch(
            self.project, self.processor, 'gpt-6-luna', 'medium',
        )
        begin_attempt(self.store, self.project, self.parent['job_id'], 1)
        self.fingerprint = _legacy_observation_fingerprint(
            project_key(self.project), self.processor, [self.source['id']],
        )
        self.store._connection.execute(
            "UPDATE observation_jobs SET model='gpt-5.6-luna',input_fingerprint=? WHERE id=?",
            (self.fingerprint, self.parent['job_id']),
        )
        self.store.fail_observation_batch(
            self.project, self.parent['job_id'], self.parent['lease_token'],
            code='invalid_response', reason_code='jev_quality_uncertain',
        )
        self.selector = dict(
            retry_job_id=self.parent['job_id'], retry_error_code='invalid_response',
            retry_attempt_count=1, retry_input_fingerprint=self.fingerprint,
            retry_previous_profile=True,
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def claim(self, store=None, **changes):
        options = {**self.selector, **changes}
        return (store or self.store).claim_observation_batch(
            self.project, self.processor, 'gpt-6-luna', 'medium', **options,
        )

    def test_successor_retains_original_profile_and_receipts(self):
        old_row = tuple(self.store._connection.execute(
            'SELECT * FROM observation_jobs WHERE id=?', (self.parent['job_id'],),
        ).fetchone())
        old_usage = [tuple(row) for row in self.store._connection.execute(
            'SELECT * FROM observer_usage_attempts WHERE job_id=?', (self.parent['job_id'],),
        )]
        old_status = self.store.observation_job_status(self.project, self.parent['job_id'])
        successor = self.claim()
        self.assertNotEqual(self.parent['job_id'], successor['job_id'])
        self.assertEqual('gpt-6-luna', successor['model'])
        self.assertEqual(1, successor['attempt_count'])
        self.assertEqual([self.source['id']], [row['id'] for row in successor['sources']])
        self.assertEqual(old_row, tuple(self.store._connection.execute(
            'SELECT * FROM observation_jobs WHERE id=?', (self.parent['job_id'],),
        ).fetchone()))
        self.assertEqual(old_usage, [tuple(row) for row in self.store._connection.execute(
            'SELECT * FROM observer_usage_attempts WHERE job_id=?', (self.parent['job_id'],),
        )])
        status = self.store.observation_job_status(self.project, self.parent['job_id'])
        self.assertEqual(old_status['failure_receipts'], status['failure_receipts'])
        self.assertEqual(successor['job_id'], status['successor_job_id'])
        self.assertEqual('running', status['successor_status'])
        self.assertIsNone(self.claim())
        self.store.fail_observation_batch(
            self.project, successor['job_id'], successor['lease_token'],
            code='invalid_response', reason_code='jev_quality_rejected',
        )
        self.assertEqual('failed', self.store.observation_job_status(
            self.project, self.parent['job_id'])['successor_status'])
        self.assertIsNone(self.claim())

    def test_stale_selectors_and_no_opt_in_never_fall_back(self):
        self.store.remember(self.project, 'Unrelated', 'Fresh capture.', source='hook:Stop')
        for changes in (
            dict(retry_previous_profile=False), dict(retry_attempt_count=2),
            dict(retry_input_fingerprint='a' * 64), dict(retry_error_code='timeout'),
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(self.claim(**changes))
        self.assertEqual(1, self.store.status(self.project)['observation_jobs']['jobs'])
        with self.assertRaises(ValueError):
            self.claim(retry_input_fingerprint=None)

    def test_live_project_lane_blocks_successor(self):
        self.store.remember(self.project, 'Another turn', 'Fresh capture.',
                            source='hook:Stop', session_id='another-session')
        other = self.store.claim_observation_batch(
            self.project, self.processor, 'gpt-6-luna', 'medium',
        )
        self.assertIsNotNone(other)
        self.assertIsNone(self.claim())

    def test_superseded_or_deleted_sources_reject_snapshot(self):
        self.store._connection.execute(
            'UPDATE entries SET superseded_by=? WHERE id=?',
            (self.source['id'], self.source['id']),
        )
        self.assertIsNone(self.claim())
        self.store._connection.execute('DELETE FROM entries WHERE id=?', (self.source['id'],))
        self.assertIsNone(self.claim())

    def test_parallel_stores_consume_permission_once(self):
        second = Store(self.root / 'data')
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(self.claim, [self.store, second]))
            self.assertEqual(1, sum(result is not None for result in results))
            self.assertEqual(1, self.store._connection.execute(
                'SELECT COUNT(*) FROM observation_job_recoveries',
            ).fetchone()[0])
        finally:
            second.close()

    def assert_one_shot_exhaustion(self, job):
        self.assertEqual('recovery_one_shot', job['disposition'])
        begin_attempt(self.store, self.project, job['job_id'], job['attempt_count'])
        self.store._connection.execute(
            "UPDATE observation_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?",
            (job['job_id'],),
        )
        for options in ({}, {'retry_failed': True}):
            self.assertIsNone(self.store.claim_observation_batch(
                self.project, self.processor, 'gpt-6-luna', 'medium', **options,
            ))
        exhausted = self.store.observation_job_status(self.project, job['job_id'])
        self.assertEqual('failed', exhausted['status'])
        self.assertEqual('recovery_exhausted', exhausted['disposition'])
        self.assertEqual('lease_expired', exhausted['error_code'])
        self.assertEqual(job['attempt_count'], exhausted['attempt_count'])
        self.assertEqual([], exhausted['output_ids'])
        self.assertIsNone(exhausted['lease_expires_at'])
        self.assertEqual(('lease_expired', 'lease_expired'), tuple(
            self.store._connection.execute(
                'SELECT outcome,error_code FROM observer_usage_attempts WHERE job_id=? AND attempt_count=?',
                (job['job_id'], job['attempt_count']),
            ).fetchone(),
        ))
        fingerprint = self.store._connection.execute(
            'SELECT input_fingerprint FROM observation_jobs WHERE id=?', (job['job_id'],),
        ).fetchone()[0]
        renewed = self.store.claim_observation_batch(
            self.project, self.processor, 'gpt-6-luna', 'medium',
            retry_job_id=job['job_id'], retry_error_code='lease_expired',
            retry_attempt_count=job['attempt_count'], retry_input_fingerprint=fingerprint,
            retry_one_shot=True,
        )
        self.assertEqual(job['job_id'], renewed['job_id'])
        self.assertEqual(job['attempt_count'] + 1, renewed['attempt_count'])

    def test_legacy_one_shot_crash_cannot_automatically_retry(self):
        self.assert_one_shot_exhaustion(self.claim(retry_one_shot=True))

    def test_current_one_shot_crash_cannot_automatically_retry(self):
        fingerprint = _observation_fingerprint(
            project_key(self.project), self.processor, 'gpt-6-luna', 'medium', [self.source['id']],
        )
        self.store._connection.execute(
            "UPDATE observation_jobs SET model='gpt-6-luna',input_fingerprint=? WHERE id=?",
            (fingerprint, self.parent['job_id']),
        )
        job = self.claim(retry_previous_profile=False, retry_input_fingerprint=fingerprint,
                         retry_one_shot=True)
        self.assertEqual(self.parent['job_id'], job['job_id'])
        self.assert_one_shot_exhaustion(job)

    def test_one_shot_requires_exact_guard(self):
        with self.assertRaises(ValueError):
            self.store.claim_observation_batch(
                self.project, self.processor, 'gpt-6-luna', 'medium', retry_one_shot=True,
            )

    def test_relation_write_failure_rolls_back_successor(self):
        self.store._connection.execute(
            "CREATE TRIGGER deny_recovery BEFORE INSERT ON observation_job_recoveries "
            "BEGIN SELECT RAISE(ABORT,'test recovery failure'); END",
        )
        with self.assertRaises(StoreError):
            self.claim()
        self.assertEqual(1, self.store.status(self.project)['observation_jobs']['jobs'])
        self.assertEqual(0, self.store._connection.execute(
            'SELECT COUNT(*) FROM observation_job_recoveries',
        ).fetchone()[0])
        self.store._connection.execute('DROP TRIGGER deny_recovery')
        self.assertIsNotNone(self.claim())

    def test_success_is_recorded_on_successor_only(self):
        successor = self.claim()
        self.store.finish_observation_batch(
            self.project, successor['job_id'], successor['lease_token'],
            notes=[{'title': 'Verified requirement', 'body': 'Requirement is ready.'}],
        )
        parent = self.store.observation_job_status(self.project, self.parent['job_id'])
        self.assertEqual('failed', parent['status'])
        self.assertEqual('processed', parent['successor_status'])
        self.assertEqual(1, self.store.status(self.project)['observation_jobs']['failed'])


if __name__ == '__main__':
    unittest.main()
