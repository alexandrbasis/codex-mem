"""Offline outcome contract and exact rejected-attempt recovery controls."""
import copy
import json
import unittest

from codex_mem import summary_outcome as outcome
from codex_mem.summary_support import SupportError, safe_diagnostic
from tests import test_summary_outcome as outcome_fixtures
from tests.test_summary_support import outcome_output


class OutcomeRecoveryContractTests(unittest.TestCase):
    setUp = outcome_fixtures.OutcomeContractTests.setUp
    prepare_bound = outcome_fixtures.OutcomeContractTests.prepare_bound

    def test_schema_rejects_invalid_states_for_each_source_availability(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest('optional jsonschema is unavailable')
        _, schema, payload = self.prepare_bound()
        validator = Draft202012Validator(schema)
        output = outcome_output(payload)
        available = next(pair for pair in payload['relations'] if pair['source_item_id'] == 'p1')
        unavailable = next(pair for pair in payload['relations'] if pair['source_item_id'] == 'observation_language')
        valid_common = {('absent', 'not_compared', 'not_compared'),
            ('unresolved', 'unresolved', 'unresolved'), ('present', 'disjoint', 'not_compared'),
            ('present', 'unresolved', 'unresolved')}
        for pair, aligned_values in ((available, ('compatible', 'incompatible', 'unresolved')),
                                     (unavailable, ('not_established',))):
            target = next(row for row in output['rows'] if row['relation_id'] == pair['id'])
            expected = valid_common | {('present', 'aligned', value) for value in aligned_values}
            for app in ('absent', 'present', 'unresolved'):
                for scope in ('aligned', 'disjoint', 'unresolved', 'not_compared'):
                    for value in ('compatible', 'incompatible', 'unresolved', 'not_established', 'not_compared'):
                        target.update(applicability=app, scope_relation=scope, value=value)
                        self.assertEqual((app, scope, value) in expected, validator.is_valid(output),
                                         (pair, app, scope, value))
            target.update(applicability='absent', scope_relation='not_compared', value='not_compared')

    def test_outcome_verdict_diagnostics_are_fixed_and_historical_receipts_stay_readable(self):
        _, _, payload = self.prepare_bound()
        cases = [
            ('p1', 'absent', 'aligned', 'compatible', 'outcome_absent_state'),
            ('p1', 'unresolved', 'aligned', 'compatible', 'outcome_unresolved_state'),
            ('p1', 'present', 'disjoint', 'compatible', 'outcome_disjoint_state'),
            ('p1', 'present', 'unresolved', 'compatible', 'outcome_scope_unresolved_state'),
            ('p1', 'present', 'aligned', 'not_established', 'outcome_available_value'),
            ('observation_language', 'present', 'aligned', 'incompatible', 'outcome_unavailable_value'),
            ('p1', 'PRIVATE_MODEL_TEXT', 'aligned', 'compatible', 'outcome_unknown_state'),
        ]
        from codex_mem.summary_support import diagnostic
        for item, app, scope, value, expected_rule in cases:
            with self.subTest(rule=expected_rule):
                output = outcome_output(payload)
                pair = next(p for p in payload['relations'] if p['source_item_id'] == item)
                row = next(r for r in output['rows'] if r['relation_id'] == pair['id'])
                row.update(applicability=app, scope_relation=scope, value=value)
                with self.assertRaises(SupportError) as caught:
                    outcome.validate_outcome(output, payload)
                self.assertEqual(expected_rule, caught.exception.rule_code)
                self.assertEqual(pair['unit_id'], caught.exception.unit_id)
                detail = diagnostic(self.summary, self.claim, status='invalid', invalid_subtype='verdict',
                    version=outcome.OUTCOME_VERSION, rule_code=caught.exception.rule_code,
                    unit_id=caught.exception.unit_id)
                self.assertEqual(detail, safe_diagnostic(detail))
                self.assertNotIn('PRIVATE_MODEL_TEXT', json.dumps(detail))
                legacy = {key: value for key, value in detail.items() if key not in ('rule_code', 'unit_id')}
                self.assertEqual(legacy, safe_diagnostic(legacy))
                for bad in (dict(detail, rule_code='PRIVATE_MODEL_TEXT'), dict(detail, unit_id='private-field')):
                    with self.assertRaises(ValueError): safe_diagnostic(bad)


class ReviewFixtures:
    setUp = outcome_fixtures.SummaryOutcomeTests.setUp
    capture = outcome_fixtures.SummaryOutcomeTests.capture
    generate = outcome_fixtures.SummaryOutcomeTests.generate
    stored = outcome_fixtures.SummaryOutcomeTests.stored


class MalformedReviewProcessorTests(ReviewFixtures, unittest.TestCase):
    def test_successful_malformed_review_has_specific_durable_reason_and_no_outputs(self):
        from codex_mem.processor import process_pending
        from tests.test_summary_support import supported_review
        self.capture()
        def review(request):
            result = supported_review(request)
            if request['version'] == outcome.OUTCOME_VERSION:
                result['output']['rows'][0].update(applicability='absent', scope_relation='aligned', value='compatible')
            return result
        result = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=review)
        self.assertEqual('summary_review_malformed', result.get('reason_code'), result)
        detail = result['summary_support'][-1]
        self.assertEqual(('invalid', 'verdict', 'outcome_absent_state', 'title'),
            tuple(detail[key] for key in ('status', 'invalid_subtype', 'rule_code', 'unit_id')))
        from codex_mem.store import Store
        with Store(self.data) as store:
            job = store.observation_job_status(self.project, result['job_id'])
            self.assertEqual([], job['output_ids'])
            self.assertEqual('summary_review_malformed', job['failure_receipts'][-1]['reason_code'])
            attempt = store.observation_attempts(self.project, result['job_id'])[0]
            call = json.loads(attempt['execution_calls'])['summary_outcome_1']
            self.assertEqual('success', call['outcome'])
            self.assertEqual(detail, call['review_diagnostic'])

    def test_malformed_review_cannot_hide_changed_source_or_write_any_packet_records(self):
        from codex_mem.processor import process_pending
        from codex_mem.store import Store
        from tests.test_summary_support import supported_review
        self.capture()
        def generate(request):
            result = self.generate(request)
            result['output']['notes'] = [dict(title='Fixture finding', body='A synthetic reported result.',
                tags=[], source_ids=[request['sources'][0]['id']])]
            return result
        def review(request):
            result = supported_review(request)
            if request['version'] == outcome.OUTCOME_VERSION:
                result['output']['rows'][0].update(applicability='absent', scope_relation='aligned', value='compatible')
                with Store(self.data) as store:
                    store._connection.execute("UPDATE entries SET body=? WHERE source='hook:Stop'",
                        ('Changed retained evidence.',))
            return result
        result = process_pending(self.project, self.data, runner=generate, summary_reviewer=review)
        self.assertEqual('summary_support_stale', result.get('reason_code'), result)
        with Store(self.data) as store:
            job = store.observation_job_status(self.project, result['job_id'])
            self.assertEqual([], job['output_ids'])
            self.assertEqual('summary_support_stale', job['failure_receipts'][-1]['reason_code'])
            self.assertEqual([], [record for record in store.timeline(self.project)
                                  if record['source'].startswith('processor:')])

    def test_preparation_and_internal_validation_errors_stay_blocking(self):
        from unittest import mock
        from codex_mem.processor import process_pending
        from tests.test_summary_support import supported_review
        from codex_mem.config import configure
        for stage, error in (('prepare_outcome', SupportError('unknown_span')),
                             ('validate_outcome', KeyError('private internal detail'))):
            with self.subTest(stage=stage):
                self.data = self.data.parent / stage
                configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                    jev_filter_enabled=False, jev_quality_enabled=False)
                self.capture()
                with mock.patch('codex_mem.summary_outcome.' + stage, side_effect=error):
                    result = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=supported_review)
                self.assertEqual('summary_support_invalid', result.get('reason_code'), result)
                self.assertNotIn('private internal detail', json.dumps(result))


class MalformedReviewServiceTests(ReviewFixtures, unittest.TestCase):

    def malformed(self):
        from codex_mem.processor import process_pending
        from tests.test_summary_support import supported_review
        def review(request):
            result = supported_review(request)
            if request['version'] == outcome.OUTCOME_VERSION:
                result['output']['rows'][0].update(applicability='absent', scope_relation='aligned', value='compatible')
            return result
        return process_pending(self.project, self.data, runner=self.generate, summary_reviewer=review)

    def configure_service(self):
        from codex_mem.config import configure
        configure(self.data, capture_scope='all', service_enabled=True, semantic_enabled=False,
                  jev_filter_enabled=False, jev_quality_enabled=False)

    def block_as_historical(self):
        from codex_mem.service import enqueue, run_service
        from codex_mem.store import Store
        self.capture()
        result = self.malformed()
        self.configure_service()
        # Exact historical fixture: preserve the successful reviewer call and
        # its five-key invalid-verdict receipt, under the old general reason.
        with Store(self.data) as store:
            attempt = store.observation_attempts(self.project, result['job_id'])[0]
            calls = json.loads(attempt['execution_calls'])
            detail = calls['summary_outcome_1']['review_diagnostic']
            detail.pop('rule_code'); detail.pop('unit_id')
            store._connection.execute('UPDATE observer_usage_attempts SET execution_calls=? WHERE job_id=?',
                                      (json.dumps(calls), result['job_id']))
            store._connection.execute('UPDATE observation_failure_receipts SET reason_code=? WHERE job_id=?',
                                      ('summary_support_invalid', result['job_id']))
        enqueue(self.project, self.data, clock=lambda: 1000)
        blocked = run_service(self.data, processor=lambda *_a, **_k: dict(result, reason_code='summary_support_invalid'),
            processor_workers=1, clock=lambda: 1000, sleeper=lambda _: None, max_cycles=1)
        self.assertEqual('halted', blocked['status'])
        return result

    def test_malformed_batch_quarantines_and_fresh_session_continues(self):
        from codex_mem.service import enqueue, run_service, service_status
        from codex_mem.store import Store
        from codex_mem.processor import process_pending
        from tests.test_summary_support import receipt
        self.capture()
        self.configure_service()
        enqueue(self.project, self.data, clock=lambda: 1000)
        seen = []
        def process(project, **kwargs):
            seen.append(kwargs['retry_failed'])
            if len(seen) == 1:
                result = self.malformed()
                with Store(self.data) as store:
                    store.remember(self.project, 'Fresh evidence', 'Independent fresh source.',
                        source='hook:PostToolUse', session_id='fresh-session')
                self.failed_job = result['job_id']
                return result
            return process_pending(project, self.data, runner=lambda _: receipt(dict(
                disposition='skipped', notes=[], session_summary=None)))
        result = run_service(self.data, processor=process, processor_workers=1,
            clock=lambda: 1000, sleeper=lambda _: None, max_cycles=5)
        self.assertNotEqual('halted', result['status'], result)
        self.assertEqual([False, False, False], seen)
        status = service_status(self.data, clock=lambda: 1000)
        self.assertEqual(0, status['blocked_projects'])
        self.assertEqual(1, status['quarantined_batches'])
        with Store(self.data) as store:
            job = store.observation_job_status(self.project, self.failed_job)
            self.assertEqual(('failed', 1, []), (job['status'], job['attempt_count'], job['output_ids']))

    def test_explicit_resume_requires_exact_historical_completed_review_proof(self):
        from codex_mem.service import resume_pending, SERVICE_STATE_FILENAME
        from codex_mem.store import Store
        result = self.block_as_historical()
        state_before = (self.data / SERVICE_STATE_FILENAME).read_text()
        with Store(self.data) as store:
            attempt = store.observation_attempts(self.project, result['job_id'])[0]
            original_calls = json.loads(attempt['execution_calls'])
        cases = ('stale_attempt', 'running_attempt', 'failed_call', 'mismatched_identity',
                 'mismatched_profile', 'invalid_diagnostic', 'later_checked', 'support_only', 'queue_other_job',
                 'queue_missing', 'reason_mismatch', 'missing_finished_at')
        for change in cases:
            with self.subTest(change=change):
                (self.data / SERVICE_STATE_FILENAME).write_text(state_before)
                calls = copy.deepcopy(original_calls)
                call = calls['summary_outcome_1']
                with Store(self.data) as store:
                    if change == 'failed_call': call['outcome'] = 'failed'
                    if change == 'mismatched_identity': call['actual_identity']['model'] = 'other-model'
                    if change == 'mismatched_profile': call['requested_profile']['timeout'] = 17
                    if change == 'invalid_diagnostic': call['review_diagnostic']['extra'] = 'private text'
                    store._connection.execute('UPDATE observer_usage_attempts SET finished_at=? WHERE job_id=?',
                        (None if change == 'missing_finished_at' else attempt['finished_at'], result['job_id']))
                    if change == 'later_checked':
                        later = copy.deepcopy(call); later['call_id'] = 'summary_outcome_2'
                        later['review_diagnostic'].update(status='checked', invalid_subtype=None)
                        calls['summary_outcome_2'] = later
                    if change == 'support_only':
                        calls.pop('summary_outcome_1')
                        calls['summary_support_1']['review_diagnostic'].update(status='invalid', invalid_subtype='verdict')
                    store._connection.execute('UPDATE observer_usage_attempts SET execution_calls=?, outcome=? WHERE job_id=?',
                        (json.dumps(calls), 'running' if change == 'running_attempt' else 'failed', result['job_id']))
                    store._connection.execute('UPDATE observation_jobs SET attempt_count=? WHERE id=?',
                        (2 if change == 'stale_attempt' else 1, result['job_id']))
                if change in ('queue_other_job', 'queue_missing', 'reason_mismatch'):
                    state = json.loads(state_before)
                    state['projects'][str(self.project.resolve())]['last_failure_job'] = '0' * 32
                    if change == 'queue_missing': state['projects'] = {}
                    if change == 'reason_mismatch':
                        state['projects'][str(self.project.resolve())]['last_rejected_reason'] = 'invalid_output_shape'
                    (self.data / SERVICE_STATE_FILENAME).write_text(json.dumps(state))
                recovery = resume_pending(self.project, self.data, rejected_job_id=result['job_id'], clock=lambda: 1000)
                self.assertEqual('blocked', recovery['status'], change)
        (self.data / SERVICE_STATE_FILENAME).write_text(state_before)
        with Store(self.data) as store:
            store._connection.execute('UPDATE observer_usage_attempts SET finished_at=? WHERE job_id=?',
                (attempt['finished_at'], result['job_id']))
            store._connection.execute('UPDATE observer_usage_attempts SET execution_calls=?, outcome=? WHERE job_id=?',
                (json.dumps(original_calls), 'failed', result['job_id']))
        recovery = resume_pending(self.project, self.data, rejected_job_id=result['job_id'], clock=lambda: 1000)
        self.assertEqual('queued', recovery['status'], recovery)
        self.assertFalse(recovery['retry_failed'])
        with Store(self.data) as store:
            job = store.observation_job_status(self.project, result['job_id'])
            self.assertEqual(('failed', 1, []), (job['status'], job['attempt_count'], job['output_ids']))

    def test_source_malformed_after_support_correction_quarantines_current_batch(self):
        from unittest import mock
        from codex_mem.processor import NativeProcessorRunner, process_pending
        from codex_mem.service import enqueue, run_service, service_status
        from codex_mem.store import Store
        from tests.test_summary_support import supported_review, comparison, receipt
        self.capture()
        self.configure_service()
        enqueue(self.project, self.data, clock=lambda: 1000)
        review_versions = []
        def review(request):
            review_versions.append(request['version'])
            result = supported_review(request)
            if request['version'] == 'summary-support-v6' and review_versions.count(request['version']) == 1:
                comparison(result['output'])['verdict'] = 'contradicted'
            if request['version'] == 'summary-source-v4': result['output'] = {}
            return result
        def generate(instance, request):
            instance.before_dispatch()
            instance.usage_checkpoint(dict(worker_thread_id='fixture-thread', worker_turn_id='fixture-turn',
                identity={key: request['execution_profile'][key]
                    for key in ('runtime', 'provider', 'model', 'reasoning_effort')}))
            candidate = self.generate(request)['output']
            self.assertIsInstance(instance.candidate_review(candidate, 240), str)
            candidate['session_summary']['completed'] = 'Corrected attributed fixture result.'
            instance.candidate_review(candidate, 240)
            self.fail('Malformed source review must reject before generation returns')
        calls = []
        def process(project, **kwargs):
            calls.append(kwargs['retry_failed'])
            if len(calls) == 1:
                result = process_pending(project, self.data, summary_reviewer=review)
                self.assertEqual('summary_review_malformed', result.get('reason_code'), result)
                with Store(self.data) as store:
                    attempt = store.observation_attempts(project, result['job_id'])[0]
                    ledger = json.loads(attempt['execution_calls'])
                    self.assertEqual('supported', ledger['summary_support_2']['review_diagnostic']['status'])
                    self.assertEqual('invalid', ledger['summary_source_1']['review_diagnostic']['status'])
                    self.assertNotIn('summary_outcome_1', ledger)
                self.failed_job = result['job_id']
                return result
            return process_pending(project, self.data, runner=lambda _: receipt(dict(
                disposition='skipped', notes=[], session_summary=None)))
        with mock.patch.object(NativeProcessorRunner, 'check_profile'), \
             mock.patch.object(NativeProcessorRunner, 'run', generate):
            result = run_service(self.data, processor=process, processor_workers=1,
                clock=lambda: 1000, sleeper=lambda _: None, max_cycles=4)
        self.assertNotEqual('halted', result['status'], result)
        self.assertEqual([False, False], calls)
        self.assertEqual(['summary-support-v6', 'summary-support-v6', 'summary-source-v4'], review_versions)
        self.assertEqual(0, service_status(self.data, clock=lambda: 1000)['blocked_projects'])
        with Store(self.data) as store:
            job = store.observation_job_status(self.project, self.failed_job)
            self.assertEqual(('failed', 1, []), (job['status'], job['attempt_count'], job['output_ids']))

    def block_as_historical_support(self, *, corrected):
        from unittest import mock
        from codex_mem.processor import NativeProcessorRunner, process_pending
        from codex_mem.service import enqueue, run_service
        from codex_mem.store import Store
        from tests.test_summary_support import supported_review, comparison
        self.capture()
        support_count = 0
        def review(request):
            nonlocal support_count
            result = supported_review(request)
            self.assertEqual('summary-support-v6', request['version'])
            support_count += 1
            if corrected and support_count == 1:
                comparison(result['output'])['verdict'] = 'contradicted'
            else:
                comparison(result['output'])['evidence_span_ids'] = []
            return result
        def generate(instance, request):
            instance.before_dispatch()
            instance.usage_checkpoint(dict(worker_thread_id='fixture-thread', worker_turn_id='fixture-turn',
                identity={key: request['execution_profile'][key]
                    for key in ('runtime', 'provider', 'model', 'reasoning_effort')}))
            candidate = self.generate(request)['output']
            feedback = instance.candidate_review(candidate, 240)
            self.assertTrue(corrected)
            self.assertIsInstance(feedback, str)
            candidate['session_summary']['completed'] = 'Corrected attributed fixture result.'
            instance.candidate_review(candidate, 240)
            self.fail('Malformed verdict must reject before generation returns')
        with mock.patch.object(NativeProcessorRunner, 'check_profile'), \
             mock.patch.object(NativeProcessorRunner, 'run', generate):
            result = process_pending(self.project, self.data, summary_reviewer=review)
        self.assertEqual('summary_review_malformed', result.get('reason_code'), result)
        self.assertEqual(2 if corrected else 1, support_count)
        with Store(self.data) as store:
            attempt = store.observation_attempts(self.project, result['job_id'])[0]
            calls = json.loads(attempt['execution_calls'])
            detail = calls['summary_support_2' if corrected else 'summary_support_1']['review_diagnostic']
            self.assertEqual('verdict', detail['invalid_subtype'])
            detail.pop('rule_code'); detail.pop('unit_id')
            store._connection.execute('UPDATE observer_usage_attempts SET execution_calls=? WHERE job_id=?',
                (json.dumps(calls), result['job_id']))
            store._connection.execute('UPDATE observation_failure_receipts SET reason_code=? WHERE job_id=?',
                ('summary_support_invalid', result['job_id']))
        self.configure_service()
        enqueue(self.project, self.data, clock=lambda: 1000)
        blocked = run_service(self.data, processor=lambda *_a, **_k: dict(result, reason_code='summary_support_invalid'),
            processor_workers=1, clock=lambda: 1000, sleeper=lambda _: None, max_cycles=1)
        self.assertEqual('halted', blocked['status'])
        return result

    def test_explicit_resume_accepts_exact_historical_support_verdict_without_retry(self):
        from codex_mem.config import configure
        from codex_mem.processor import process_pending
        from codex_mem.service import resume_pending, run_service
        from codex_mem.store import Store
        for corrected in (False, True):
            with self.subTest(corrected=corrected):
                self.data = self.data.parent / ('historical-support-' + str(corrected))
                configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                    jev_filter_enabled=False, jev_quality_enabled=False)
                result = self.block_as_historical_support(corrected=corrected)
                with Store(self.data) as store:
                    before_job = store.observation_job_status(self.project, result['job_id'])
                    before_attempts = store.observation_attempts(self.project, result['job_id'])
                    before_sources = store.timeline(self.project)
                recovery = resume_pending(self.project, self.data, rejected_job_id=result['job_id'], clock=lambda: 1000)
                self.assertEqual('queued', recovery['status'], recovery)
                self.assertFalse(recovery['retry_failed'])
                retry_flags = []
                def process(project, **kwargs):
                    retry_flags.append(kwargs['retry_failed'])
                    return process_pending(project, self.data,
                        runner=lambda _: self.fail('Historical failed sources must not be retried'))
                run_service(self.data, processor=process, processor_workers=1,
                    clock=lambda: 1000, sleeper=lambda _: None, max_cycles=2)
                self.assertEqual([False], retry_flags)
                with Store(self.data) as store:
                    self.assertEqual(before_job, store.observation_job_status(self.project, result['job_id']))
                    self.assertEqual(before_attempts, store.observation_attempts(self.project, result['job_id']))
                    self.assertEqual(before_sources, store.timeline(self.project))

    def test_historical_support_resume_rejects_other_failures_and_superseded_proof(self):
        from codex_mem.service import resume_pending, SERVICE_STATE_FILENAME
        from codex_mem.store import Store
        result = self.block_as_historical_support(corrected=True)
        state_before = (self.data / SERVICE_STATE_FILENAME).read_text()
        with Store(self.data) as store:
            before_attempts = store.observation_attempts(self.project, result['job_id'])
        original_calls = json.loads(before_attempts[0]['execution_calls'])
        other_subtypes = ('structure', 'unit_coverage', 'unknown_span', 'bounds',
                          'unknown_proposition', 'duplicate_reference')
        cases = (*other_subtypes, 'old_support_version', 'source_verdict', 'missing_identity',
                 'failed_review', 'unknown_reason', 'stale_reason', 'newer_block',
                 'later_supported_outcome', 'source_after_invalid_support')
        for change in cases:
            with self.subTest(change=change):
                state = json.loads(state_before)
                calls = copy.deepcopy(original_calls)
                call = calls['summary_support_2']
                reason = 'summary_support_invalid'
                if change in other_subtypes: call['review_diagnostic']['invalid_subtype'] = change
                if change == 'old_support_version': call['review_diagnostic']['version'] = 'summary-support-v5'
                if change == 'missing_identity': call['actual_identity'] = None
                if change == 'failed_review': call['outcome'] = 'failed'
                if change == 'unknown_reason': reason = None
                if change == 'stale_reason':
                    reason = 'summary_support_stale'
                    state['projects'][str(self.project.resolve())]['last_rejected_reason'] = reason
                if change == 'newer_block':
                    state['projects'][str(self.project.resolve())]['last_failure_job'] = '0' * 32
                if change == 'later_supported_outcome':
                    later = copy.deepcopy(call)
                    later['call_id'] = 'summary_outcome_2'
                    later['review_diagnostic'].update(version='summary-outcome-v5', status='checked', invalid_subtype=None)
                    calls[later['call_id']] = later
                if change in ('source_verdict', 'source_after_invalid_support'):
                    source = copy.deepcopy(call)
                    source['call_id'] = 'summary_source_1'
                    source['review_diagnostic']['version'] = 'summary-source-v4'
                    if change == 'source_after_invalid_support':
                        source['review_diagnostic'].update(status='interpreted', invalid_subtype=None)
                    else:
                        calls.pop('summary_support_2')
                    calls[source['call_id']] = source
                with Store(self.data) as store:
                    store._connection.execute('UPDATE observer_usage_attempts SET execution_calls=? WHERE job_id=?',
                        (json.dumps(calls), result['job_id']))
                    store._connection.execute('UPDATE observation_failure_receipts SET reason_code=? WHERE job_id=?',
                        (reason, result['job_id']))
                (self.data / SERVICE_STATE_FILENAME).write_text(json.dumps(state))
                recovery = resume_pending(self.project, self.data, rejected_job_id=result['job_id'], clock=lambda: 1000)
                self.assertEqual('blocked', recovery['status'], (change, recovery))
                with Store(self.data) as store:
                    after = store.observation_job_status(self.project, result['job_id'])
                    self.assertEqual(('failed', 1, []), (after['status'], after['attempt_count'], after['output_ids']))
                    self.assertEqual(json.dumps(calls), store.observation_attempts(self.project, result['job_id'])[0]['execution_calls'])
