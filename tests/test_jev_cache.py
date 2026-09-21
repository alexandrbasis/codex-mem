"""Exact-input cache reuse is isolated, content-free and revalidated."""
import copy
import json
import tempfile
import unittest
from unittest import mock

from codex_mem import jev_cache as cache
from codex_mem import jev_filter as jf
from codex_mem.store import Store


def response():
    return {"model": jf.MODEL, "answers": {
        "useful": {"type": "noul", "noul": .9},
        "category": {"type": "choice", "choice": "decision", "confidence": .99,
                     "probabilities": {key: int(key == 'decision') for key in jf.CATEGORIES}}},
        "usage": {"input_tokens": 100, "output_tokens": 2}}


class JevCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.project = '/tmp/jev-cache-project'
        self.payload = {'model': jf.MODEL, 'questions': copy.deepcopy(jf.QUESTIONS),
                        'state': {'source_fragment': 'PRIVATE RAW TEXT', 'source_id': 'PRIVATE_SOURCE_ID',
                                  'summary_required': True, 'context': 'prior decision'}}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_restart_reuses_validated_response_without_new_usage(self):
        self.assertIsNone(cache.cache_get(self.store, self.project, self.payload))
        cache.cache_put(self.store, self.project, self.payload, response())
        self.store.close()
        self.store = Store(self.tmp.name)
        found = cache.cache_get(self.store, self.project, self.payload)
        self.assertEqual(found['answers'], response()['answers'])
        self.assertEqual(found['usage'], {'input_tokens': 0, 'output_tokens': 0})

    def test_project_isolation(self):
        cache.cache_put(self.store, self.project, self.payload, response())
        self.assertIsNone(cache.cache_get(self.store, '/tmp/other-project', self.payload))

    def test_every_input_and_policy_affects_key(self):
        base = cache.payload_key(self.payload)
        for path, value in [(('state', 'source_fragment'), 'changed'), (('state', 'context'), 'other history'),
                            (('state', 'summary_required'), False), (('model',), 'jev-1.14.0'),
                            (('questions', 'useful', 'instructions'), 'changed judgment')]:
            changed = copy.deepcopy(self.payload)
            node = changed
            for part in path[:-1]:
                node = node[part]
            node[path[-1]] = value
            with self.subTest(path=path):
                self.assertNotEqual(base, cache.payload_key(changed))
        self.assertNotEqual(base, cache.payload_key(self.payload, policy_version='next-policy'))
        self.assertEqual(base, cache.payload_key(dict(reversed(list(self.payload.items())))))

    def test_no_raw_or_arbitrary_response_metadata_persisted(self):
        raw = response()
        raw['credential'] = 'PRIVATE_SECRET'
        raw['answers']['useful']['text'] = 'PRIVATE_SECRET'
        raw['answers']['category']['reason'] = 'PRIVATE_SECRET'
        raw['usage']['metadata'] = 'PRIVATE_SECRET'
        cache.cache_put(self.store, self.project, self.payload, raw)
        row = self.store._connection.execute('SELECT * FROM jev_evaluation_cache').fetchone()
        serialized = json.dumps(tuple(row))
        for secret in ('PRIVATE RAW TEXT', 'PRIVATE_SOURCE_ID', 'PRIVATE_SECRET', 'prior decision'):
            self.assertNotIn(secret, serialized)
        self.assertEqual(len(row['cache_key']), 64)

    def test_invalid_responses_never_cached(self):
        invalid = []
        for path, value in [(('model',), 'jev-latest'), (('answers', 'useful', 'noul'), float('nan')),
                            (('answers', 'category', 'confidence'), float('inf')),
                            (('answers', 'category', 'probabilities'), {'routine': 1})]:
            raw = response()
            node = raw
            for part in path[:-1]:
                node = node[part]
            node[path[-1]] = value
            invalid.append(raw)
        invalid.extend([{}, {'error': 'failed'}])
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(jf.JevFilterError):
                cache.cache_put(self.store, self.project, self.payload, raw)
        self.assertIsNone(cache.cache_get(self.store, self.project, self.payload))

    def test_corrupt_cache_is_a_miss(self):
        cache.cache_put(self.store, self.project, self.payload, response())
        for corrupt in ('not JSON', '{}', '{"model":"jev-latest"}', 'null'):
            self.store._write(lambda: self.store._connection.execute(
                'UPDATE jev_evaluation_cache SET response_json=?', (corrupt,)))
            self.assertIsNone(cache.cache_get(self.store, self.project, self.payload))

    def test_project_local_bounded_pruning(self):
        with mock.patch.object(cache, 'MAX_PROJECT_ENTRIES', 2):
            cache.cache_put(self.store, '/tmp/other-project', self.payload, response())
            for text in ('first', 'second', 'third'):
                self.payload['state']['source_fragment'] = text
                cache.cache_put(self.store, self.project, self.payload, response())
        self.assertEqual(self.store._connection.execute('SELECT COUNT(*) FROM jev_evaluation_cache').fetchone()[0], 3)
        self.payload['state']['source_fragment'] = 'first'
        self.assertIsNone(cache.cache_get(self.store, self.project, self.payload))

    def test_wrong_model_and_question_ids_cannot_hit(self):
        for key, value in [('model', 'jev-latest'), ('questions', {'other': {}})]:
            changed = copy.deepcopy(self.payload)
            changed[key] = value
            self.assertIsNone(cache.cache_get(self.store, self.project, changed))
            with self.assertRaises(jf.JevFilterError):
                cache.cache_put(self.store, self.project, changed, response())
