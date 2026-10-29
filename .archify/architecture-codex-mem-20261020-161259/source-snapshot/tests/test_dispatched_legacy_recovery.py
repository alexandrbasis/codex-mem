"""Explicit legacy successor recovery; normal dispatch uncertainty stays closed."""
import tempfile
import unittest
from pathlib import Path

from codex_mem.processor import MODEL, PROCESSOR_ID, REASONING_EFFORT
from codex_mem.store import Store, StoreError


class DispatchedLegacyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        self.store = Store(Path(self.temp.name) / 'data')
        self.addCleanup(self.store.close)
        self.raw = self.store.remember(self.project, 'Captured result', 'A bounded test result.',
                                       source='hook:PostToolUse', session_id='session')
        self.parent = self.store.claim_observation_batch(self.project, PROCESSOR_ID, 'gpt-5.6-luna', REASONING_EFFORT)
        self.store.fail_observation_batch(self.project, self.parent['job_id'], self.parent['lease_token'],
            code='protocol_error', worker_thread_id='old-thread', worker_turn_id='old-turn')
        self.before = dict(self.store._connection.execute('SELECT * FROM observation_jobs WHERE id=?',
                                                        (self.parent['job_id'],)).fetchone())
        self.selector = dict(retry_job_id=self.parent['job_id'], retry_error_code='protocol_error',
            retry_attempt_count=1, retry_input_fingerprint=self.before['input_fingerprint'],
            retry_previous_profile=True, retry_one_shot=True)

    def claim(self, **changes):
        return self.store.claim_observation_batch(self.project, PROCESSOR_ID, MODEL, REASONING_EFFORT,
            **dict(self.selector, **changes))

    def test_explicit_worker_identity_preserves_closed_parent_and_creates_only_one_successor(self):
        self.assertIsNone(self.claim())
        child = self.claim(retry_dispatched_worker=('old-thread', 'old-turn'))
        self.assertIsNotNone(child)
        self.assertNotEqual(self.parent['job_id'], child['job_id'])
        after = dict(self.store._connection.execute('SELECT * FROM observation_jobs WHERE id=?',
                                                   (self.parent['job_id'],)).fetchone())
        self.assertEqual(self.before, after)
        self.assertEqual([self.raw['id']], [row['id'] for row in child['sources']])
        with self.assertRaises(StoreError):
            self.store.finish_observation_batch(self.project, self.parent['job_id'],
                self.parent['lease_token'], notes=[], disposition='skipped')
        self.store.finish_observation_batch(self.project, child['job_id'], child['lease_token'],
                                            notes=[], disposition='skipped')
        self.assertIsNone(self.claim(retry_dispatched_worker=('old-thread', 'old-turn')))

    def test_changed_identity_or_snapshot_cannot_dispatch(self):
        for changes in (dict(retry_dispatched_worker=('different', 'old-turn')),
                        dict(retry_dispatched_worker=('old-thread', 'different')),
                        dict(retry_dispatched_worker=('old-thread', 'old-turn'), retry_attempt_count=2),
                        dict(retry_dispatched_worker=('old-thread', 'old-turn'), retry_input_fingerprint='0'*64)):
            with self.subTest(changes=changes):
                self.assertIsNone(self.claim(**changes))
        self.assertEqual(1, self.store._connection.execute('SELECT count(*) FROM observation_jobs').fetchone()[0])

    def test_dispatched_exception_requires_exact_one_shot_legacy_successor(self):
        for changes in (dict(retry_previous_profile=False), dict(retry_input_fingerprint=None),
                        dict(retry_one_shot=False), dict(retry_error_code='invalid_response'),
                        dict(retry_dispatched_worker=('old-thread', None))):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.claim(**{'retry_dispatched_worker': ('old-thread', 'old-turn'), **changes})
