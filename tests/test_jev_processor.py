"""End-to-end eligibility boundary before the observation generator."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem.config import configure
from codex_mem.processor import process_pending, _effective_lease_seconds
from codex_mem.store import Store
from tests.test_jev_filter import answer
from tests import test_processor as processor_fixtures


class JevProcessorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.home = self.root / 'memory'
        configure(self.home, jev_filter_enabled=True)

    def remember(self, text, source='hook:PostToolUse'):
        with Store(self.home) as store:
            return store.remember(self.project, 'Event', text, source=source, session_id='jev-test')

    @staticmethod
    def skipped(_):
        return processor_fixtures.ProcessorTests.receipt({'disposition': 'skipped', 'notes': []})

    @staticmethod
    def note(request):
        return processor_fixtures.ProcessorTests.receipt({'disposition': 'processed', 'notes': [{
            'title': 'Verified decision', 'body': 'Use local storage for offline access.',
            'tags': [], 'source_ids': [request['sources'][0]['id']]}]})

    def status(self, result):
        with Store(self.home) as store:
            return store.observation_job_status(self.project, result['job_id'])

    def usage_count(self, job_id):
        with Store(self.home) as store:
            exists = store._connection.execute("SELECT 1 FROM sqlite_master WHERE name='observer_usage_attempts'").fetchone()
            return store._connection.execute('SELECT COUNT(*) FROM observer_usage_attempts WHERE job_id=?', (job_id,)).fetchone()[0] if exists else 0

    def test_mixed_sources_and_raw_history_are_screened_before_generator(self):
        history = self.remember('REJECT_HISTORY_SENTINEL')
        configure(self.home, jev_filter_enabled=False)
        process_pending(self.project, self.home, runner=self.skipped)
        configure(self.home, jev_filter_enabled=True)
        rejected = self.remember('REJECT_CURRENT_SENTINEL')
        accepted = self.remember('KEEP_DECISION_SENTINEL')
        evaluated = []
        def evaluate(payload):
            item = json.loads(payload['state']['source_fragment'])
            evaluated.append(item['id'])
            return answer(0.01, 'routine') if 'REJECT_' in item['body'] else answer()
        def runner(request):
            self.assertEqual(['s1'], [item['id'] for item in request['sources']])
            self.assertEqual('KEEP_DECISION_SENTINEL', request['sources'][0]['body'])
            self.assertNotIn('REJECT_', json.dumps(request))
            return self.note(request)
        result = process_pending(self.project, self.home, runner=runner, jev_evaluator=evaluate)
        self.assertEqual('processed', result['status'], result)
        self.assertEqual({history['id'], rejected['id'], accepted['id']}, set(evaluated))
        job = self.status(result)
        with Store(self.home) as store:
            outputs = store.get(self.project, job['output_ids'])
            self.assertEqual([accepted['id']], outputs[0]['source_ids'])
            self.assertEqual('REJECT_CURRENT_SENTINEL', store.get(self.project, [rejected['id']])[0]['body'])
        self.assertTrue(job['jev_filter_attempts'][0]['generator_started'])
        self.assertNotIn('SENTINEL', json.dumps(job))

    def test_all_rejected_skips_without_generation_or_fake_usage(self):
        raw = self.remember('Opened a routine file.')
        runner = mock.Mock(side_effect=AssertionError('Generator must not run'))
        result = process_pending(self.project, self.home, runner=runner,
                                 jev_evaluator=lambda _: answer(0.01, 'routine'))
        self.assertEqual('skipped', result['status'], result)
        runner.assert_not_called()
        self.assertEqual(0, self.usage_count(result['job_id']))
        self.assertFalse(self.status(result)['jev_filter_attempts'][0]['generator_started'])
        with Store(self.home) as store:
            original = store.get(self.project, [raw['id']])[0]
            self.assertEqual(raw['body'], original['body'])
            self.assertIsNone(original['superseded_by'])
        self.assertEqual('idle', process_pending(self.project, self.home, runner=runner)['status'])

    def test_transport_and_malformed_failures_preserve_sources_and_record_audit(self):
        for malformed in (False, True):
            with self.subTest(malformed=malformed):
                self.home = self.root / ('malformed' if malformed else 'transport')
                configure(self.home, jev_filter_enabled=True)
                raw = self.remember('PRIVATE_RAW_SENTINEL')
                def evaluate(_):
                    if malformed:
                        return {}
                    raise RuntimeError('PRIVATE_API_KEY_SENTINEL')
                runner = mock.Mock(side_effect=AssertionError('Generator must not run'))
                result = process_pending(self.project, self.home, runner=runner, jev_evaluator=evaluate)
                self.assertEqual('failed', result['status'], result)
                runner.assert_not_called()
                self.assertEqual(0, self.usage_count(result['job_id']))
                job = self.status(result)
                audit = job['jev_filter_attempts'][0]
                self.assertEqual('failure', audit['status'])
                self.assertEqual('jev_filter_invalid_response' if malformed else 'jev_filter_transport', audit['error_code'])
                self.assertFalse(audit['generator_started'])
                self.assertNotIn('PRIVATE_', json.dumps([result, job]))
                with Store(self.home) as store:
                    original = store.get(self.project, [raw['id']])[0]
                    self.assertEqual(raw['body'], original['body'])
                    self.assertIsNone(original['superseded_by'])

    def test_rejected_stop_is_only_a_marker_for_required_summary(self):
        self.remember('Use local storage for offline access.')
        earlier = process_pending(self.project, self.home, runner=self.note, jev_evaluator=lambda _: answer())
        self.assertEqual('processed', earlier['status'], earlier)
        previous_note_ids = set(self.status(earlier)['output_ids'])
        stop = self.remember('REJECT_STOP_SENTINEL', 'hook:Stop')
        evaluated = []
        def evaluate(payload):
            item = json.loads(payload['state']['source_fragment'])
            evaluated.append(item['id'])
            return answer(0.01, 'routine') if item['id'] == stop['id'] else answer()
        def runner(request):
            self.assertNotIn('REJECT_STOP_SENTINEL', json.dumps(request))
            self.assertIn('Use local storage', request['prompt'])
            self.assertEqual('Session boundary', request['sources'][0]['title'])
            summary = processor_fixtures.ProcessorTests.structured_summary(request['sources'][0]['id'])
            return processor_fixtures.ProcessorTests.receipt({'disposition': 'processed', 'notes': [], 'session_summary': summary})
        result = process_pending(self.project, self.home, runner=runner, jev_evaluator=evaluate)
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(1, result['session_summary_count'])
        self.assertEqual([stop['id']], result['jev_filter']['lifecycle_only_ids'])
        self.assertEqual(stop['id'], evaluated[0])
        self.assertTrue(previous_note_ids.issubset(set(evaluated)))
        self.assertFalse(self.status(result)['jev_filter_attempts'][0]['history_skipped'])

    def test_disabled_and_outside_scope_keep_existing_behavior(self):
        for enabled, scope in ((False, []), (True, [str(self.root / 'elsewhere')])):
            with self.subTest(enabled=enabled):
                self.home = self.root / ('disabled' if not enabled else 'outside')
                configure(self.home, jev_filter_enabled=enabled, jev_filter_projects=scope)
                self.remember('UNFILTERED_SENTINEL')
                evaluator = mock.Mock(side_effect=AssertionError('Out of scope'))
                runner = mock.Mock(side_effect=self.skipped)
                result = process_pending(self.project, self.home, runner=runner, jev_evaluator=evaluator)
                self.assertEqual('skipped', result['status'], result)
                evaluator.assert_not_called()
                self.assertIn('UNFILTERED_SENTINEL', runner.call_args.args[0]['prompt'])
                self.assertEqual([], self.status(result)['jev_filter_attempts'])

    def test_required_summary_cannot_replace_rejected_derived_note_with_raw_history(self):
        raw = self.remember('Original decision evidence.')
        earlier = process_pending(self.project, self.home, runner=self.note, jev_evaluator=lambda _: answer())
        self.assertEqual('processed', earlier['status'], earlier)
        derived_ids = set(self.status(earlier)['output_ids'])
        unrelated = self.remember('UNRELATED_RAW_HISTORY')
        configure(self.home, jev_filter_enabled=False)
        process_pending(self.project, self.home, runner=self.skipped)
        configure(self.home, jev_filter_enabled=True)
        self.remember('REJECTED_STOP_TEXT', 'hook:Stop')
        evaluated = []
        def evaluate(payload):
            item = json.loads(payload['state']['source_fragment'])
            evaluated.append(item['id'])
            return answer(0.01, 'routine') if item['id'] in derived_ids else answer()
        runner = mock.Mock(side_effect=AssertionError('Summary must not run without prior notes'))
        result = process_pending(self.project, self.home, runner=runner, jev_evaluator=evaluate)
        self.assertEqual('failed', result['status'], result)
        runner.assert_not_called()
        self.assertTrue(derived_ids.issubset(set(evaluated)))
        self.assertIn(unrelated['id'], evaluated)
        self.assertEqual(0, self.usage_count(result['job_id']))
        audit = self.status(result)['jev_filter_attempts'][0]
        self.assertFalse(audit['generator_started'])
        decisions = {item['source_id']: item['route'] for item in audit['decisions']}
        self.assertEqual('retain', decisions[unrelated['id']])
        self.assertTrue(all(decisions[item] == 'discard' for item in derived_ids))

    def test_note_cannot_cite_a_rejected_stop_lifecycle_marker(self):
        accepted = self.remember('Accepted decision evidence.')
        stop = self.remember('REJECTED_STOP_TEXT', 'hook:Stop')
        def evaluate(payload):
            item = json.loads(payload['state']['source_fragment'])
            return answer(0.01, 'routine') if item['id'] == stop['id'] else answer()
        def runner(request):
            self.assertNotIn('REJECTED_STOP_TEXT', json.dumps(request))
            marker = next(item['id'] for item in request['sources'] if item['title'] == 'Session boundary')
            return processor_fixtures.ProcessorTests.receipt({
                'disposition': 'processed',
                'notes': [{'title': 'Invented finding', 'body': 'A marker is not evidence.',
                           'tags': [], 'source_ids': [marker]}],
                'session_summary': processor_fixtures.ProcessorTests.structured_summary(marker),
            })
        result = process_pending(self.project, self.home, runner=runner, jev_evaluator=evaluate)
        self.assertEqual('failed', result['status'], result)
        self.assertEqual('invalid_response', result['code'])
        job = self.status(result)
        self.assertEqual([], job['output_ids'])
        self.assertTrue(job['jev_filter_attempts'][0]['generator_started'])
        with Store(self.home) as store:
            sources = store.get(self.project, [accepted['id'], stop['id']])
            self.assertTrue(all(item['superseded_by'] is None for item in sources))

    def test_enabled_filter_extends_lease_by_its_deadline(self):
        self.remember('Routine.')
        with mock.patch('codex_mem.processor._effective_lease_seconds', wraps=_effective_lease_seconds) as lease:
            result = process_pending(self.project, self.home, timeout=30, lease_seconds=1,
                                     runner=self.skipped, jev_evaluator=lambda _: answer(0.01, 'routine'))
        self.assertEqual('skipped', result['status'], result)
        lease.assert_called_once_with(1, 90)
        self.assertEqual(100, _effective_lease_seconds(1, 90))

    def test_discarded_new_ack_skips_raw_history_and_generator(self):
        history = self.remember('HISTORY_SHOULD_NOT_BE_EVALUATED')
        configure(self.home, jev_filter_enabled=False)
        process_pending(self.project, self.home, runner=self.skipped)
        configure(self.home, jev_filter_enabled=True)
        ack = self.remember('ok thanks', 'hook:UserPromptSubmit')
        seen = []
        def evaluate(payload):
            item = json.loads(payload['state']['source_fragment'])
            seen.append(item['id'])
            self.assertNotIn('HISTORY_SHOULD_NOT_BE_EVALUATED', json.dumps(payload))
            return answer(0.01, 'routine')
        runner = mock.Mock(side_effect=AssertionError('No new eligible evidence'))
        result = process_pending(self.project, self.home, runner=runner, jev_evaluator=evaluate)
        self.assertEqual('skipped', result['status'], result)
        self.assertEqual([ack['id']], seen)
        self.assertNotIn(history['id'], seen)
        runner.assert_not_called()
        self.assertEqual(0, self.usage_count(result['job_id']))
        audit = self.status(result)['jev_filter_attempts'][0]
        self.assertTrue(audit['history_skipped'])
        self.assertEqual(1, audit['counts']['requests'])

    def test_failed_generator_retry_reuses_persisted_exact_jev_answers(self):
        self.remember('CACHE_SOURCE_SENTINEL')
        evaluator = mock.Mock(return_value=answer())
        broken_runner = mock.Mock(side_effect=RuntimeError('Transient generation error'))
        first = process_pending(self.project, self.home, runner=broken_runner, jev_evaluator=evaluator)
        self.assertEqual('failed', first['status'], first)
        self.assertEqual('runner_failure', first['code'])
        self.assertEqual(1, evaluator.call_count)
        # process_pending closes its Store; the retry must read a persisted row.
        second_evaluator = mock.Mock(side_effect=AssertionError('Exact input must be cached'))
        second = process_pending(self.project, self.home, retry_failed=True,
                                 runner=self.note, jev_evaluator=second_evaluator)
        self.assertEqual('processed', second['status'], second)
        self.assertEqual(first['job_id'], second['job_id'])
        second_evaluator.assert_not_called()
        audit = self.status(second)['jev_filter_attempts'][-1]
        self.assertEqual(2, audit['attempt_count'])
        self.assertEqual(0, audit['counts']['requests'])
        self.assertEqual(1, audit['counts']['cache_hits'])
        self.assertEqual({'input_tokens': 0, 'output_tokens': 0}, audit['usage'])
        self.assertEqual('cache', audit['decisions'][0]['chunks'][0]['evaluation_source'])
        with Store(self.home) as store:
            rows = store._connection.execute('SELECT response_json FROM jev_evaluation_cache').fetchall()
            self.assertTrue(rows)
            self.assertNotIn('CACHE_SOURCE_SENTINEL', json.dumps([row[0] for row in rows]))
