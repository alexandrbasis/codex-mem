"""Eligibility is conservative, complete, and fails closed before generation."""
import copy
import json
import unittest
from unittest import mock

from codex_mem import jev_filter as jf


def answer(useful=0.9, category="decision", confidence=0.95):
    return {"model": jf.MODEL, "answers": {
        "useful": {"type": "noul", "noul": useful},
        "category": {"type": "choice", "choice": category, "confidence": confidence,
                     "probabilities": {key: 1.0 if key == category else 0.0 for key in jf.CATEGORIES}},
    }, "usage": {"input_tokens": 12, "output_tokens": 2}}


def claim(body="We chose a local database because offline access is required."):
    return {"sources": [{"id": "s1", "title": "Decision", "body": body, "source": "hook:Stop"}],
            "context": [{"id": "c1", "title": "History", "body": "Earlier context", "source": "hook:PostToolUse"}],
            "summary_required": True, "project_context": "Derived project context"}


class JevFilterTests(unittest.TestCase):
    def test_elapsed_gate_time_is_reported_for_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), mock.patch.object(jf.time, 'perf_counter', side_effect=[10, 10.125]):
                if fail:
                    with self.assertRaises(jf.JevFilterError) as caught:
                        jf.filter_claim(claim(), evaluator=lambda _: {})
                    audit = caught.exception.audit
                else:
                    _, audit = jf.filter_claim(claim(), evaluator=lambda _: answer())
                self.assertEqual(125, audit.get('duration_ms'))

    def test_filters_sources_and_context_independently_and_preserves_order(self):
        source = claim()
        source["sources"].append({"id": "s2", "title": "Routine", "body": "Opened file"})
        seen = []
        def evaluate(payload):
            seen.append(payload)
            item = json.loads(payload["state"]["source_fragment"])
            return answer(0.1, "routine") if item["id"] in ("s2", "c1") else answer()
        result, audit = jf.filter_claim(source, evaluator=evaluate)
        self.assertEqual([x["id"] for x in result["sources"]], ["s1"])
        self.assertEqual(result["context"], [])
        self.assertEqual(result["project_context"], source["project_context"])
        self.assertEqual(len(source["sources"]), 2)
        self.assertEqual(audit["counts"], {"evaluated": 3, "retained": 1, "discarded": 2, "chunks": 3, "requests": 3, "cache_hits": 0})
        self.assertEqual(audit["usage"], {"input_tokens": 36, "output_tokens": 6})
        self.assertEqual(seen[0]["state"]["evidence_role"], "assistant_report")
        self.assertTrue(seen[0]["state"]["summary_required"])
        self.assertNotIn("Earlier context", json.dumps(audit))
        self.assertNotIn(source["sources"][0]["body"], json.dumps(audit))

    def test_uncertainty_retained_and_all_routine_discarded(self):
        for useful, category, confidence, kept in [(0.2, "routine", 0.8, False),
                (0.21, "routine", 0.99, True), (0.01, "routine", 0.79, True),
                (0.01, "other", 1, True), (0.01, "open_work", 1, True)]:
            with self.subTest(useful=useful, category=category, confidence=confidence):
                result, _ = jf.filter_claim(claim(), evaluator=lambda p: answer(useful, category, confidence))
                self.assertEqual(bool(result["sources"]), kept)
                self.assertEqual(bool(result["context"]), kept)

    def test_large_source_all_bytes_evaluated_and_useful_middle_retains_whole(self):
        original = claim('界"\\' * 15000 + "MIDDLE_DECISION" + "x" * 80000)
        original["context"] = []
        original["sources"][0]["tool_io"] = {"stdout": "LAST_TOOL_OUTPUT"}
        fragments = []
        def evaluate(payload):
            self.assertLessEqual(len(jf._encode(payload)), jf.MAX_PAYLOAD_BYTES)
            fragment = payload["state"]["source_fragment"]
            fragments.append(fragment)
            return answer() if "MIDDLE_DECISION" in fragment else answer(0.01, "routine")
        result, audit = jf.filter_claim(original, evaluator=evaluate)
        self.assertGreater(len(fragments), 5)
        self.assertEqual(json.loads("".join(fragments)), original["sources"][0])
        self.assertEqual(result["sources"], original["sources"])
        self.assertEqual(audit["counts"]["chunks"], len(fragments))

    def test_invalid_responses_fail_closed(self):
        invalid = []
        for path, value in [(('model',), 'jev-latest'), (('answers', 'useful', 'noul'), float('nan')),
                (('answers', 'useful', 'noul'), True), (('answers', 'category', 'confidence'), float('inf')),
                (('answers', 'category', 'choice'), 'unknown'), (('usage', 'input_tokens'), True),
                (('answers', 'category', 'probabilities'), {'routine': 1}),
                (('answers', 'category', 'probabilities'), {key: 0 for key in jf.CATEGORIES})]:
            value_response = answer()
            target = value_response
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            invalid.append(value_response)
        missing = answer()
        del missing['answers']['useful']
        invalid.extend([missing, {}, None, {'raw': 'PRIVATE_SOURCE'}])
        for response in invalid:
            with self.subTest(response=response), self.assertRaises(jf.JevFilterError) as error:
                jf.filter_claim(claim(), evaluator=lambda p: response)
            self.assertEqual(str(error.exception), 'jev_filter_invalid_response')

    def test_transport_exception_does_not_leak_raw_data(self):
        def fail(payload):
            raise ValueError('PRIVATE_SOURCE and PRIVATE_API_KEY')
        with self.assertRaises(jf.JevFilterError) as error:
            jf.filter_claim(claim(), evaluator=fail)
        self.assertEqual(str(error.exception), 'jev_filter_transport')

    def test_failure_in_later_chunk_does_not_return_partial_claim(self):
        count = 0
        def evaluate(payload):
            nonlocal count
            count += 1
            return answer() if count == 1 else {}
        with self.assertRaises(jf.JevFilterError):
            jf.filter_claim(claim('x' * 80000), evaluator=evaluate)
        self.assertEqual(count, 2)

    def test_new_redaction_applies_to_request_and_returned_source(self):
        secret = 'apikey_' + 'a' * 32 + '_' + 'b' * 64
        source = claim(secret)
        source['sources'][0]['tool_io'] = {'stdout': secret}
        def evaluate(payload):
            self.assertNotIn(secret, json.dumps(payload))
            return answer()
        result, _ = jf.filter_claim(source, evaluator=evaluate)
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(source['sources'][0]['body'], secret)

    def test_redirects_rejected(self):
        with self.assertRaises(jf.JevFilterError):
            jf._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test')

    def test_credentials_missing_safe(self):
        with mock.patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(jf.JevFilterError) as error:
                jf.filter_claim(claim())
        self.assertEqual(str(error.exception), 'jev_filter_credentials')

    def test_credentials_file_and_fixed_request_shape(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / 'key'
            key_file.write_text('test-token')
            with mock.patch.dict('os.environ', {}, clear=True):
                self.assertEqual(jf._credentials(str(key_file)), 'test-token')
        payload = next(jf._payloads(claim()['sources'][0], 'sources', True))
        self.assertEqual(set(payload), {'model', 'state', 'questions'})
        self.assertEqual(payload['questions']['useful']['criteria'].keys(), {'true', 'false'})

    def test_http_transport_posts_only_to_fixed_endpoint_and_reads_bounded_response(self):
        response = mock.MagicMock()
        response.status = 200
        response.read1.return_value = json.dumps(answer()).encode()
        response.isclosed.return_value = True
        response.__enter__.return_value = response
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(jf, '_credentials', return_value='private-token'), \
                mock.patch.object(jf.urllib.request, 'build_opener', return_value=opener):
            result = jf._post({'model': jf.MODEL}, jf.time.monotonic() + 5)
        self.assertEqual(result, answer())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, jf.ENDPOINT)
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.get_header('Authorization'), 'Bearer private-token')
        self.assertEqual(response.read1.call_count, 1)
        self.assertLessEqual(response.read1.call_args.args[0], 8192)

    def test_production_parallelism_is_bounded_and_failure_waits_for_workers(self):
        import threading
        import time
        lock = threading.Lock()
        state = {'active': 0, 'peak': 0, 'started': 0}
        original = claim()
        original['sources'] = [dict(original['sources'][0], id=f's{i}') for i in range(8)]
        def post(payload, deadline, key_file):
            with lock:
                state['active'] += 1
                state['started'] += 1
                state['peak'] = max(state['peak'], state['active'])
                first = state['started'] == 1
            try:
                time.sleep(0.025)
                if first:
                    raise jf.JevFilterError('jev_filter_transport')
                return answer()
            finally:
                with lock:
                    state['active'] -= 1
        with mock.patch.object(jf, '_post', side_effect=post):
            with self.assertRaises(jf.JevFilterError):
                jf.filter_claim(original)
        self.assertEqual(state['active'], 0)
        self.assertGreater(state['peak'], 1)
        self.assertLessEqual(state['peak'], 4)

    def test_derived_context_redacted_before_generator(self):
        original = claim()
        original['project_context'] = 'apikey_' + 'a' * 32 + '_' + 'b' * 64
        result, _ = jf.filter_claim(original, evaluator=lambda p: answer())
        self.assertNotEqual(result['project_context'], original['project_context'])

    def test_rounded_probabilities_preserve_actual_values(self):
        for minor in (0.01, 0.03):
            response = answer()
            probabilities = {key: 0.02 for key in jf.CATEGORIES}
            probabilities['decision'] = 0.86
            probabilities['routine'] = minor
            response['answers']['category']['probabilities'] = probabilities
            result, audit = jf.filter_claim(claim(), evaluator=lambda p: response)
            self.assertTrue(result['sources'])
            self.assertEqual(audit['decisions'][0]['chunks'][0]['probabilities'], probabilities)

    def test_partial_failure_keeps_completed_audit_and_usage(self):
        calls = 0
        def evaluate(payload):
            nonlocal calls
            calls += 1
            response = answer()
            if calls == 2:
                response['answers'] = {}
            return response
        with self.assertRaises(jf.JevFilterError) as error:
            jf.filter_claim(claim(), evaluator=evaluate)
        audit = error.exception.audit
        self.assertTrue(audit['incomplete'])
        self.assertEqual(audit['usage_status'], 'partial')
        self.assertEqual(audit['usage'], {'input_tokens': 24, 'output_tokens': 4})
        self.assertEqual(audit['counts']['evaluated'], 1)
        self.assertEqual(audit['decisions'][0]['source_id'], 's1')
        self.assertNotIn('local database', json.dumps(audit))

    def test_partial_source_has_incomplete_route_and_does_not_count_as_retained(self):
        calls = 0
        def evaluate(payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError('private failure')
            return answer()
        with self.assertRaises(jf.JevFilterError) as error:
            jf.filter_claim(claim('x' * 80000), evaluator=evaluate)
        audit = error.exception.audit
        self.assertEqual(audit['decisions'][0]['route'], 'incomplete')
        self.assertEqual(audit['counts'], {'evaluated': 0, 'retained': 0, 'discarded': 0, 'chunks': 1, 'requests': 2, 'cache_hits': 0})
        self.assertEqual(audit['usage'], {'input_tokens': 12, 'output_tokens': 2})

    def test_noise_sources_skip_history_unless_summary_required(self):
        for required, calls in ((False, 1), (True, 2)):
            original = claim()
            original['summary_required'] = required
            evaluator = mock.Mock(return_value=answer(0.1, 'routine'))
            result, audit = jf.filter_claim(original, evaluator=evaluator)
            self.assertEqual(evaluator.call_count, calls)
            self.assertEqual(audit['history_skipped'], not required)
            self.assertEqual(result['context'], [])

    def test_cached_repeat_has_no_requests_or_paid_usage_and_exact_payload_changes_miss(self):
        saved = {}
        key = lambda payload: json.dumps(payload, sort_keys=True)
        def put(payload, response):
            saved[key(payload)] = copy.deepcopy(response)
        evaluator = mock.Mock(return_value=answer())
        kwargs = dict(evaluator=evaluator, cache_get=lambda p: saved.get(key(p)), cache_put=put)
        _, first = jf.filter_claim(claim(), **kwargs)
        _, second = jf.filter_claim(claim(), **kwargs)
        self.assertEqual(first['counts']['requests'], 2)
        self.assertEqual(second['counts']['requests'], 0)
        self.assertEqual(second['counts']['cache_hits'], 2)
        self.assertEqual(second['usage'], {'input_tokens': 0, 'output_tokens': 0})
        self.assertEqual(second['decisions'][0]['chunks'][0]['evaluation_source'], 'cache')
        changed = claim('Different content')
        _, third = jf.filter_claim(changed, **kwargs)
        self.assertEqual(third['counts']['requests'], 1)
        changed['summary_required'] = False
        _, fourth = jf.filter_claim(changed, **kwargs)
        self.assertEqual(fourth['counts']['requests'], 2)

    def test_cache_callbacks_stay_on_caller_thread_with_parallel_http(self):
        import threading
        caller = threading.get_ident()
        threads = []
        def get(payload):
            self.assertEqual(threading.get_ident(), caller)
        def put(payload, response):
            self.assertEqual(threading.get_ident(), caller)
        def post(*args):
            threads.append(threading.get_ident())
            return answer()
        with mock.patch.object(jf, '_post', side_effect=post):
            jf.filter_claim(claim(), cache_get=get, cache_put=put)
        self.assertTrue(threads)
        self.assertTrue(all(thread != caller for thread in threads))

    def test_corrupt_cache_misses_and_only_successful_calls_cached_on_partial_failure(self):
        original = claim()
        original['sources'].append(dict(original['sources'][0], id='s2'))
        saved = []
        count = 0
        def evaluate(payload):
            nonlocal count
            count += 1
            return answer() if count == 1 else {}
        with self.assertRaises(jf.JevFilterError) as error:
            jf.filter_claim(original, evaluator=evaluate, cache_get=lambda p: {'bad': 'PRIVATE'},
                            cache_put=lambda p, r: saved.append((p, r)))
        self.assertEqual(len(saved), 1)
        self.assertEqual(error.exception.audit['counts']['requests'], 2)
        self.assertEqual(error.exception.audit['counts']['cache_hits'], 0)

    def test_categories_are_independent_of_retention_and_location_not_in_state(self):
        self.assertIn('verification_result', jf.CATEGORIES)
        self.assertIn('problem', jf.CATEGORIES)
        self.assertNotIn('verified_finding', jf.CATEGORIES)
        source = claim()['sources'][0]
        self.assertEqual(list(jf._payloads(source, 'sources', False)), list(jf._payloads(source, 'context', False)))
        for category in ('verification_result', 'problem'):
            result, _ = jf.filter_claim(claim(), evaluator=lambda p: answer(0.01, category))
            self.assertTrue(result['sources'])

    def test_acknowledgement_metadata_retained_as_evidence_but_no_threshold_bypass(self):
        original = claim('Готово')
        original['summary_required'] = False
        original['context'] = []
        original['sources'][0].update(created_at='2026-09-19T00:00:00Z', access_count=50)
        def evaluate(payload):
            source = json.loads(payload['state']['source_fragment'])
            self.assertEqual(source['created_at'], '2026-09-19T00:00:00Z')
            self.assertEqual(source['body'], 'Готово')
            return answer(0.09, 'routine', 0.79)
        result, audit = jf.filter_claim(original, evaluator=evaluate)
        self.assertTrue(result['sources'])
        self.assertFalse(audit['history_skipped'])
        self.assertEqual(jf.POLICY_VERSION, 'memory-eligibility-v3')

    def test_timeout_checked_after_evaluator(self):
        with mock.patch.object(jf.time, 'monotonic', side_effect=[0, 0, 61]):
            with self.assertRaises(jf.JevFilterError) as error:
                jf.filter_claim(claim(), evaluator=lambda p: answer())
        self.assertEqual(str(error.exception), 'jev_filter_timeout')


if __name__ == '__main__':
    unittest.main()
