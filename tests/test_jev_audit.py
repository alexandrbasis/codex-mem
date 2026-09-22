"""Jev receipts contain classifications and counters, never raw observations."""
import copy
import json
import tempfile
import unittest

from codex_mem.jev_audit import CATEGORIES, record_filter_attempt, read_filter_attempts
from codex_mem.store import ObservationLeaseExpired, Store, project_key


class JevAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.project = project_key('/tmp/jev-audit-project')
        self.source_id = self.store.remember(self.project, 'source', 'private raw observation',
                                             source='hook:UserPromptSubmit')['id']
        self.store._write(lambda: self.store._connection.execute('''
            INSERT INTO observation_jobs(id,project,processor_id,model,reasoning_effort,
                input_fingerprint,input_limit,status,attempt_count,output_ids_json,created_at,updated_at,lease_expires_at)
            VALUES ('job',?,'observer','test','low','fingerprint',100,'running',1,'[]','now','now','2099-01-01T00:00:00Z')
        ''', (self.project,)))
        self.store._write(lambda: self.store._connection.execute(
            "INSERT INTO observation_job_sources VALUES ('job',?)", (self.source_id,)))
        self.audit = {'status': 'success', 'model': 'jev-1.13.0', 'policy_version': 'memory-eligibility-v1',
                      'decisions': [{'source_id': self.source_id, 'location': 'sources', 'route': 'retain',
                                     'chunks': [{'category': 'decision', 'route': 'retain', 'confidence': .9,
                                                 'useful_probability': .8,
                                                 'probabilities': {key: int(key == 'decision') for key in CATEGORIES}}]}],
                      'usage': {'input_tokens': 10, 'output_tokens': 2}}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def write(self, audit=None, **kwargs):
        record_filter_attempt(self.store, kwargs.get('project', self.project), 'job',
                              kwargs.get('attempt', 1), self.audit if audit is None else audit)

    def test_durable_upsert_and_derived_counts(self):
        self.write()
        self.audit['generator_started'] = True
        self.audit['counts'] = {'retained': 999}
        self.write()
        self.store.close()
        self.store = Store(self.tmp.name)
        rows = read_filter_attempts(self.store._connection, 'job')
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['generator_started'])
        self.assertEqual(rows[0]['counts'], {'evaluated': 1, 'retained': 1, 'discarded': 0, 'chunks': 1})

    def test_gate_duration_is_optional_validated_and_survives_storage(self):
        self.write()
        self.assertNotIn('duration_ms', read_filter_attempts(self.store._connection, 'job')[0])
        self.audit['duration_ms'] = 125
        self.write()
        self.assertEqual(125, read_filter_attempts(self.store._connection, 'job')[0].get('duration_ms'))
        for bad in (-1, True, 'private', float('nan'), 2**63):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.write(dict(self.audit, duration_ms=bad))

    def test_short_circuit_is_content_free_and_requires_a_retained_fragment(self):
        decision = self.audit['decisions'][0]
        decision['short_circuited_chunks'] = 3
        self.audit['counts'] = {'short_circuited_chunks': 999, 'cache_hits': 0}
        self.write()
        audit = read_filter_attempts(self.store._connection, 'job')[0]
        self.assertEqual(audit['counts']['short_circuited_chunks'], 3)
        self.assertEqual(audit['counts']['chunks'], 1)
        self.assertEqual(audit['counts']['cache_hits'], 0)
        for value in (-1, True, 'private', 10001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decision['short_circuited_chunks'] = value
                self.write()
        decision['short_circuited_chunks'] = 3
        decision['route'] = 'discard'
        with self.assertRaises(ValueError):
            self.write()
        decision['route'] = 'retain'
        decision['chunks'][0]['route'] = 'discard'
        with self.assertRaises(ValueError):
            self.write()

    def test_v3_cache_metadata_and_categories_persist(self):
        from codex_mem.jev_audit import V3_CATEGORIES
        self.audit['policy_version'] = 'memory-eligibility-v3'
        self.audit['history_skipped'] = True
        self.audit['counts'] = {'requests': 0, 'cache_hits': 1}
        chunk = self.audit['decisions'][0]['chunks'][0]
        chunk.update(category='verification_result', evaluation_source='cache',
                     probabilities={key: int(key == 'verification_result') for key in V3_CATEGORIES})
        self.write()
        audit = read_filter_attempts(self.store._connection, 'job')[0]
        self.assertTrue(audit['history_skipped'])
        self.assertEqual(audit['counts']['cache_hits'], 1)
        self.assertEqual(audit['counts']['requests'], 0)
        self.assertEqual(audit['decisions'][0]['chunks'][0]['evaluation_source'], 'cache')

    def test_cross_project_stale_and_terminal_refused(self):
        for kwargs in ({'project': '/tmp/other-project'}, {'attempt': 2}, {'attempt': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.write(**kwargs)
        self.store._write(lambda: self.store._connection.execute("UPDATE observation_jobs SET status='processed'"))
        with self.assertRaises(ValueError):
            self.write()

    def test_raw_fields_are_excluded_recursively(self):
        self.audit['raw'] = 'secret credential'
        self.audit['usage']['response'] = 'secret credential'
        self.audit['decisions'][0]['text'] = 'secret credential'
        self.audit['decisions'][0]['chunks'][0]['reason'] = 'secret credential'
        self.write()
        encoded = json.dumps(read_filter_attempts(self.store._connection, 'job'))
        self.assertNotIn('secret credential', encoded)
        self.assertNotIn('private raw observation', encoded)

    def test_bad_probability_and_unknown_distribution_rejected(self):
        for value in (-1, 1.01, float('nan'), float('inf'), True, 'private raw observation'):
            audit = copy.deepcopy(self.audit)
            audit['decisions'][0]['chunks'][0]['useful_probability'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.write(audit)
        self.audit['decisions'][0]['chunks'][0]['probabilities']['secret'] = 0
        with self.assertRaises(ValueError):
            self.write()

    def test_fixed_failure_code_and_unknown_source(self):
        self.write({'status': 'failure', 'error_code': 'jev_filter_transport', 'exception': 'private'})
        self.assertEqual(read_filter_attempts(self.store._connection, 'job')[0]['error_code'], 'jev_filter_transport')
        with self.assertRaises(ValueError):
            self.write({'status': 'failure', 'error_code': 'private exception'})
        self.audit['decisions'][0]['source_id'] = 'private raw text pretending to be an ID'
        with self.assertRaises(ValueError):
            self.write()

    def test_last_twenty_attempts_and_read_without_schema(self):
        self.assertEqual(read_filter_attempts(self.store._connection, 'job'), [])
        for attempt in range(1, 24):
            self.store._write(lambda: self.store._connection.execute(
                "UPDATE observation_jobs SET attempt_count=?", (attempt,)))
            self.write(attempt=attempt)
        self.assertEqual([row['attempt_count'] for row in read_filter_attempts(self.store._connection, 'job')], list(range(4, 24)))

    def test_cross_project_context_id_refused(self):
        foreign = self.store.remember('/tmp/foreign-project', 'source', 'private')['id']
        self.audit['decisions'][0].update(source_id=foreign, location='context')
        with self.assertRaises(ValueError):
            self.write()

    def test_model_and_policy_cannot_store_credentials(self):
        for field in ('model', 'policy_version'):
            audit = copy.deepcopy(self.audit)
            audit[field] = 'apikey_private'
            with self.assertRaises(ValueError):
                self.write(audit)

    def test_expired_lease_cannot_record_generator_start(self):
        self.write()
        self.store._write(lambda: self.store._connection.execute(
            "UPDATE observation_jobs SET lease_expires_at='2000-01-01T00:00:00Z'"))
        self.audit['generator_started'] = True
        with self.assertRaises(ObservationLeaseExpired):
            self.write()
        self.assertFalse(read_filter_attempts(self.store._connection, 'job')[0]['generator_started'])

    def test_partial_failure_preserves_completed_chunks(self):
        self.audit.update(status='failure', incomplete=True, usage_status='partial',
                          error_code='jev_filter_timeout')
        self.audit['decisions'][0]['route'] = 'incomplete'
        self.audit['exception'] = 'private response'
        self.write()
        row = read_filter_attempts(self.store._connection, 'job')[0]
        self.assertTrue(row['incomplete'])
        self.assertEqual(row['usage_status'], 'partial')
        self.assertEqual(row['usage'], {'input_tokens': 10, 'output_tokens': 2})
        self.assertEqual(row['counts'], {'evaluated': 0, 'retained': 0, 'discarded': 0, 'chunks': 1})
        self.assertNotIn('private response', json.dumps(row))

    def test_incomplete_route_cannot_claim_success(self):
        self.audit['decisions'][0]['route'] = 'incomplete'
        with self.assertRaises(ValueError):
            self.write()

    def test_rounded_api_distribution_tolerance(self):
        self.audit['decisions'][0]['chunks'][0]['probabilities'] = {
            key: .19 if key == 'decision' else .14 for key in CATEGORIES}
        self.write()
        self.audit['decisions'][0]['chunks'][0]['probabilities']['decision'] = .25
        with self.assertRaises(ValueError):
            self.write()
