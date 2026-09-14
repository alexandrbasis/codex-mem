"""Source-first candidate lookup must preserve quarantine and project boundaries."""
from pathlib import Path
import tempfile
import unittest

from codex_mem.processor import MODEL, PROCESSOR_ID, REASONING_EFFORT
from codex_mem.store import Store


class ObservationClaimQueryTests(unittest.TestCase):
    def test_exact_storage_failure_retry_preserves_quarantine_and_foreign_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project, foreign = root / 'project', root / 'foreign'
            project.mkdir()
            foreign.mkdir()
            with Store(root / 'memory') as store:
                jobs = {}
                for name, owner, code in (('rejected', project, 'invalid_response'),
                                          ('storage', project, 'storage_failure'),
                                          ('foreign', foreign, 'storage_failure')):
                    store.remember(owner, name, name, source='hook:PostToolUse', session_id=name)
                    job = store.claim_observation_batch(owner, PROCESSOR_ID, MODEL, REASONING_EFFORT)
                    store.fail_observation_batch(owner, job['job_id'], job['lease_token'], code)
                    jobs[name] = job['job_id']
                for denied_id in (jobs['rejected'], jobs['foreign'], 'f' * 32):
                    self.assertIsNone(store.claim_observation_batch(
                        project, PROCESSOR_ID, MODEL, REASONING_EFFORT, retry_job_id=denied_id))
                claimed = store.claim_observation_batch(
                    project, PROCESSOR_ID, MODEL, REASONING_EFFORT, retry_job_id=jobs['storage'])
                self.assertEqual(jobs['storage'], claimed['job_id'])
                self.assertEqual(2, claimed['attempt_count'])
                for name in ('rejected', 'foreign'):
                    row = store._connection.execute(
                        'SELECT status,attempt_count FROM observation_jobs WHERE id=?', (jobs[name],)).fetchone()
                    self.assertEqual(('failed', 1), tuple(row))

    def test_candidate_plan_looks_up_source_before_job_and_preserves_status_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project, foreign = root / 'project', root / 'foreign'
            project.mkdir()
            foreign.mkdir()
            with Store(root / 'memory') as store:
                sources = {}
                for status in ('processed', 'skipped', 'running', 'failed'):
                    source = store.remember(project, status, status, source='hook:PostToolUse', session_id=status)
                    job = store.claim_observation_batch(project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
                    store.fail_observation_batch(project, job['job_id'], job['lease_token'], 'invalid_response')
                    store._connection.execute('UPDATE observation_jobs SET status=? WHERE id=?',
                                              (status, job['job_id']))
                    sources[status] = source['id']
                store.remember(foreign, 'foreign', 'foreign', source='hook:PostToolUse', session_id='foreign')
                foreign_job = store.claim_observation_batch(foreign, PROCESSOR_ID, MODEL, REASONING_EFFORT)
                local = store.remember(project, 'local', 'local', source='hook:PostToolUse', session_id='local')
                # A foreign project's receipt must never suppress local evidence.
                store._connection.execute('INSERT INTO observation_job_sources(job_id,source_id) VALUES(?,?)',
                                          (foreign_job['job_id'], local['id']))
                fresh = store.remember(project, 'fresh', 'fresh', source='hook:UserPromptSubmit', session_id='fresh')
                statements = []
                store._connection.set_trace_callback(statements.append)
                claimed = store.claim_observation_batch(project, PROCESSOR_ID, MODEL, REASONING_EFFORT)
                store._connection.set_trace_callback(None)
                self.assertEqual([local['id']], [r['id'] for r in claimed['sources']])
                query = next(q for q in statements if 'SELECT e.* FROM entries AS e' in q)
                plan = [r[3] for r in store._connection.execute('EXPLAIN QUERY PLAN ' + query)]
                self.assertTrue(any('observation_job_sources_source_idx (source_id=?)' in detail for detail in plan))
                self.assertFalse(any('observation_jobs_project_status_idx' in detail for detail in plan))
                # Remove only this test's latest claim so both candidate selectors
                # can be compared on the same fixture snapshot.
                store._connection.execute('DELETE FROM observation_jobs WHERE id=?', (claimed['job_id'],))
                for retry, expected in ((False, {local['id'], fresh['id']}),
                                        (True, {local['id'], fresh['id'], sources['failed']})):
                    variant = query.replace('(0 = 0 AND jobs.status',
                                            f'({int(retry)} = 0 AND jobs.status')
                    actual = {r['id'] for r in store._connection.execute(variant)}
                    original = {r['id'] for r in store._connection.execute(variant.replace('CROSS JOIN', 'JOIN'))}
                    self.assertEqual(expected, actual)
                    self.assertEqual(original, actual)


if __name__ == '__main__':
    unittest.main()
