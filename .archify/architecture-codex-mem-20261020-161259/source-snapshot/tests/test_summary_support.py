from pathlib import Path
import tempfile
import json
from unittest import mock
import unittest
from codex_mem.config import configure
from codex_mem.processor import process_pending, MODEL, REASONING_EFFORT
from codex_mem.store import Store


def receipt(output):
    return {'output': output, 'evidence': {'thread_start': {'thread_id': 'fake-thread', 'model': MODEL, 'reasoning_effort': REASONING_EFFORT, 'model_provider': 'openai'}, 'turn_started': {'thread_id': 'fake-thread', 'turn_id': 'fake-turn'}, 'turn_completed': True, 'no_tools': True, 'rerouted': False}}


def supported_review(request):
    # Deterministic wiring fixture, not a semantic quality evaluator.
    evidence = request['evidence'][-1]
    return receipt({'version': 'summary-support-v2', 'units': {
        name: dict(coverage='complete', comparisons=[dict(candidate_assertion=f'The complete synthetic {name} assertion in the input-owned unit.',
            evidence_assertion='The cited evidence supports this attributed assertion.',
            value_comparison='Same asserted value.', attribution_comparison='Same reporting role.',
            scope_comparison='Same scope and certainty.', verdict='supported',
            evidence_span_ids=[evidence['spans'][0]['id']])])
        for name in request['units']}})


def rejected_review(request):
    result = supported_review(request)
    if 'completed' in result['output']['units']:
        result['output']['units']['completed']['comparisons'][0]['verdict'] = 'contradicted'
    return result


class SummarySupportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name) / 'project'
        self.project.mkdir()
        self.data = Path(tmp.name) / 'data'
        configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False, jev_filter_enabled=False, jev_quality_enabled=False)

    def seed(self, intent='Give three Russian facts.', answer='1. Ziemia jest planetą. 2. Woda jest cieczą. 3. Słońce jest gwiazdą.'):
        with Store(self.data) as store:
            store.remember(self.project, 'Request', intent, source='hook:UserPromptSubmit', session_id='session')
            self.raw = store.remember(self.project, 'Answer', answer, source='hook:Stop', session_id='session')

    def generate(self, request):
        return receipt({'notes': [], 'disposition': 'processed', 'session_summary': {
            'title': 'Facts', 'request': 'Give three Russian facts.', 'investigated': '',
            'learned': '', 'completed': 'The assistant provided three Russian facts.',
            'next_steps': '', 'notes': '', 'source_ids': [s['id'] for s in request['sources']]}})

    def test_public_regression_requested_language_is_not_a_completed_attribute(self):
        self.seed()
        result = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=rejected_review)
        with Store(self.data) as store:
            job = store.observation_job_status(self.project, result['job_id'])
            records = store.get(self.project, job['output_ids']) if job['output_ids'] else []
        summaries = [r for r in records if r['kind'] == 'session_summary']
        self.assertFalse(summaries, 'Unsupported requested language survived processing and Store.get')

        self.assertEqual('summary_support_rejected', result['reason_code'])

    def test_contrastive_contract_rejects_false_attribute_with_valid_source_ownership(self):
        self.seed(intent='Run the checks.', answer='The assistant reported 2 checks passed; 5 were not run.')
        def generate(request):
            result = self.generate(request)
            result['output']['session_summary'].update(request='Run the checks.',
                completed='The assistant reported 5 checks passed.')
            return result
        def review(request):
            if 'units' not in request:
                # Reproduce the coarse contract's false acceptance with real quote ownership.
                return supported_review(request)
            result = supported_review(request)
            comparison = result['output']['units']['completed']['comparisons'][0]
            comparison.update(candidate_assertion='The assistant reported five passed checks.',
                evidence_assertion='The assistant reported two passed checks and five unrun checks.',
                value_comparison='Passed counts differ; unrun checks cannot count as passed.',
                verdict='contradicted')
            return result
        result = process_pending(self.project, self.data, runner=generate, summary_reviewer=review)
        self.assertEqual('summary_support_rejected', result.get('reason_code'), result)
        self.assertEqual([], self.stored(result)[0])

    def test_contrastive_value_pairs_preserve_useful_attributed_paraphrases(self):
        cases = [
            ('The assistant says two checks passed; five were not run.', 'The assistant reported 2 successful checks.', 'The assistant reported 5 successful checks.'),
            ('The assistant says /app/b.py was written; /app/a.py is untouched.', 'The assistant reported writing /app/b.py.', 'The assistant reported writing /app/a.py.'),
            ('The assistant says staging passed; production was not tested.', 'The assistant reported success in staging.', 'The assistant reported success in production.'),
            ('The first attempt failed. Correction: the later local retry passed.', 'The assistant reported that the later local retry passed.', 'The assistant reported that every attempt passed.'),
            ('The assistant says the result is partial; the rest is not visible.', 'The assistant reported a partial result.', 'The assistant reported a complete result.'),
        ]
        for index, (evidence, positive, negative) in enumerate(cases):
            for supported, candidate in ((True, positive), (False, negative)):
                with self.subTest(case=index, supported=supported):
                    self.data = self.data.parent / f'pair-{index}-{supported}'
                    configure(self.data, jev_filter_enabled=False, jev_quality_enabled=False)
                    self.seed(intent='Report the result.', answer=evidence)
                    def generate(request):
                        result = self.generate(request)
                        result['output']['session_summary'].update(title='Reported result',
                            request='Report the result.', completed=candidate)
                        return result
                    def review(request):
                        # Controlled synthetic comparison; no claim of native semantic evaluation.
                        result = supported_review(request)
                        result['output']['units']['completed']['comparisons'][0].update(
                            candidate_assertion=candidate, evidence_assertion=positive,
                            value_comparison='Same reported proposition.' if supported else 'The claimed value exceeds or differs from the reported value.',
                            verdict='supported' if supported else 'contradicted')
                        return result
                    result = process_pending(self.project, self.data, runner=generate, summary_reviewer=review)
                    records = self.stored(result)[0]
                    if supported:
                        self.assertEqual('processed', result['status'], result)
                        self.assertEqual(positive, records[0]['session_summary']['completed'])
                    else:
                        self.assertEqual('summary_support_rejected', result['reason_code'])
                        self.assertEqual([], records)

    def test_fixed_invalid_subtypes_and_fingerprints_preserve_content_privacy(self):
        for subtype in ('structure', 'unit_coverage', 'unknown_span', 'verdict', 'bounds'):
            with self.subTest(subtype=subtype):
                self.data = self.data.parent / ('diagnostic-' + subtype)
                configure(self.data, jev_filter_enabled=False, jev_quality_enabled=False)
                self.seed(answer='PRIVATE_SYNTHETIC_SOURCE_🙂')
                def review(request):
                    result = supported_review(request)
                    units = result['output']['units']
                    comparison = units['completed']['comparisons'][0]
                    if subtype == 'structure': comparison['unexpected'] = 'PRIVATE_SYNTHETIC_VERDICT'
                    elif subtype == 'unit_coverage': units.pop('title')
                    elif subtype == 'unknown_span': comparison['evidence_span_ids'] = ['PRIVATE_SYNTHETIC_SPAN']
                    elif subtype == 'verdict': comparison['verdict'] = 'PRIVATE_SYNTHETIC_VERDICT'
                    else: comparison['value_comparison'] = 'x' * 1201
                    return result
                result = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=review)
                self.assertEqual('summary_support_invalid', result['reason_code'])
                detail = result['summary_support'][0]
                self.assertEqual(subtype, detail['invalid_subtype'])
                self.assertEqual('invalid', detail['status'])
                call = json.loads(self.stored(result)[1][0]['execution_calls'])['summary_support_1']
                self.assertEqual(detail, call['review_diagnostic'])
                self.assertNotIn('PRIVATE_SYNTHETIC', json.dumps([result, call]))
                self.assertRegex(detail['candidate_fingerprint'], '^[0-9a-f]{64}$')
                self.assertRegex(detail['evidence_fingerprint'], '^[0-9a-f]{64}$')

    def test_routine_skip_remains_valid_and_does_not_dispatch_summary_review(self):
        self.seed(intent='Describe the README.', answer='The README lists the command names.')
        reviewer = mock.Mock(side_effect=AssertionError('A skipped result has no summary to review'))
        result = process_pending(self.project, self.data,
            runner=lambda _: receipt(dict(disposition='skipped', notes=[], session_summary=None)),
            summary_reviewer=reviewer)
        self.assertEqual('skipped', result['status'], result)
        reviewer.assert_not_called()

    def stored(self, result):
        with Store(self.data) as store:
            job = store.observation_job_status(self.project, result['job_id'])
            records = store.get(self.project, job['output_ids']) if job['output_ids'] else []
            attempts = store.observation_attempts(self.project, result['job_id'])
            raw = store.get(self.project, [self.raw['id']])[0]
        return records, attempts, raw

    def test_supported_result_is_useful_and_keeps_historical_verification_label(self):
        self.seed(answer='The assistant provided three Russian facts.')
        result = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=supported_review)
        self.assertEqual('processed', result['status'], result)
        records, attempts, _ = self.stored(result)
        self.assertEqual('The assistant provided three Russian facts.', records[0]['session_summary']['completed'])
        self.assertEqual('not_assessed', records[0]['provenance']['verification'])
        calls = json.loads(attempts[0]['execution_calls'])
        self.assertEqual({'generation', 'summary_support_1'}, set(calls))
        self.assertEqual(calls['generation']['requested_profile'], calls['summary_support_1']['requested_profile'])

    def test_false_status_quantity_path_and_language_are_quarantined(self):
        cases = [('Run the tests.', 'Tests were not run.', 'Tests passed.'),
                 ('Create three files.', 'Created one file.', 'Created three files.'),
                 ('Write /app/a.py.', 'Wrote /app/b.py.', 'Wrote /app/a.py.'),
                 ('Deploy production.', 'Deployed staging.', 'Deployed production.')]
        for index, (intent, answer, claim) in enumerate(cases):
            with self.subTest(claim=claim):
                self.data = self.data.parent / f'data-{index}'
                configure(self.data, jev_filter_enabled=False, jev_quality_enabled=False)
                self.seed(intent, answer)
                def generate(request):
                    output = self.generate(request)
                    output['output']['session_summary']['completed'] = claim
                    return output
                result = process_pending(self.project, self.data, runner=generate, summary_reviewer=rejected_review)
                records, _, raw = self.stored(result)
                self.assertEqual([], records)
                self.assertIsNone(raw['superseded_by'])
                self.assertEqual('summary_support_rejected', result['reason_code'])

    def test_missing_test_reviewer_never_launches_a_live_model(self):
        self.seed()
        with mock.patch('codex_mem.processor.NativeProcessorRunner', side_effect=AssertionError('live model')):
            result = process_pending(self.project, self.data, runner=self.generate)
        self.assertEqual('summary_support_unavailable', result['reason_code'])
        self.assertEqual([], self.stored(result)[0])

    def test_malformed_review_cannot_authorize_a_write(self):
        def missing(request):
            result = supported_review(request)
            result['output']['units'].pop('title')
            return result
        def invented_quote(request):
            result = supported_review(request)
            result['output']['units']['title']['comparisons'][0]['evidence_span_ids'] = ['fabricated']
            return result
        def foreign(request):
            result = supported_review(request)
            result['output']['units']['title']['comparisons'][0]['evidence_span_ids'] = ['outside-project:1']
            return result
        def bool_offset(request):
            result = supported_review(request)
            result['output']['units']['title']['comparisons'][0]['start'] = False
            return result
        for index, evaluator in enumerate((missing, invented_quote, foreign, bool_offset, lambda _: receipt({}))):
            with self.subTest(case=index):
                self.data = self.data.parent / f'invalid-{index}'
                configure(self.data, jev_filter_enabled=False, jev_quality_enabled=False)
                self.seed()
                result = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=evaluator)
                self.assertEqual('summary_support_invalid', result['reason_code'])
                self.assertEqual([], self.stored(result)[0])

    def test_review_usage_sums_once_and_independent_identity_is_retained(self):
        from codex_mem.observer_usage_store import COUNTERS
        def measured(output, thread, input_tokens, output_tokens):
            result = receipt(output)
            result['evidence']['thread_start']['thread_id'] = thread
            result['evidence']['turn_started']['thread_id'] = thread
            tokens = dict.fromkeys(COUNTERS, 0)
            tokens.update(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=input_tokens+output_tokens)
            result['metrics'] = dict(duration_ms=1, usage=dict(status='reported', source='app_server_thread_total', updates=1, tokens=tokens))
            return result
        self.seed()
        def generate(request):
            return measured(self.generate(request)['output'], 'generator', 10, 2)
        def review(request):
            return measured(rejected_review(request)['output'], 'reviewer', 20, 3)
        result = process_pending(self.project, self.data, runner=generate, summary_reviewer=review)
        records, attempts, _ = self.stored(result)
        self.assertEqual([], records)
        self.assertEqual(35, attempts[0]['total_tokens'])
        self.assertEqual(30, attempts[0]['input_tokens'])
        calls = json.loads(attempts[0]['execution_calls'])
        self.assertEqual('generator', calls['generation']['execution_id'])
        self.assertEqual('reviewer', calls['summary_support_1']['execution_id'])
        self.assertEqual(12, calls['generation']['metrics']['total_tokens'])
        self.assertEqual(23, calls['summary_support_1']['metrics']['total_tokens'])

    def test_unknown_review_counters_remain_unknown(self):
        self.seed()
        result = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=supported_review)
        _, attempts, _ = self.stored(result)
        self.assertIsNone(attempts[0]['total_tokens'])
        self.assertEqual('unavailable', attempts[0]['usage_status'])

    def test_source_change_during_review_fails_closed(self):
        self.seed()
        def review(request):
            with Store(self.data) as store:
                store._connection.execute('UPDATE entries SET body=? WHERE id=?', ('Changed source', self.raw['id']))
                store._connection.commit()
            return supported_review(request)
        result = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=review)
        self.assertEqual('summary_support_stale', result['reason_code'])
        self.assertEqual([], self.stored(result)[0])

    def test_insufficient_context_is_visible_without_claiming_a_false_outcome(self):
        self.seed()
        def review(request):
            result = supported_review(request)
            result['output']['units']['completed']['comparisons'][0].update(verdict='insufficient_context', evidence_span_ids=[])
            return result
        result = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=review)
        self.assertEqual('summary_support_uncertain', result['reason_code'])
        self.assertEqual([], self.stored(result)[0])

    def test_review_receipt_failure_retains_measured_usage_and_raw_input(self):
        self.seed()
        from codex_mem.observer_usage_store import COUNTERS
        def review(request):
            result = supported_review(request)
            result['evidence']['no_tools'] = False
            tokens = dict.fromkeys(COUNTERS, 0)
            tokens.update(input_tokens=8, output_tokens=2, total_tokens=10)
            result['metrics'] = dict(duration_ms=2, usage=dict(status='reported', source='app_server_thread_total', updates=1, tokens=tokens))
            return result
        result = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=review)
        self.assertEqual('summary_support_unavailable', result['reason_code'])
        records, attempts, raw = self.stored(result)
        self.assertEqual([], records)
        self.assertIsNone(raw['superseded_by'])
        call = json.loads(attempts[0]['execution_calls'])['summary_support_1']
        self.assertEqual(10, call['metrics']['total_tokens'])
        self.assertEqual('failed', call['outcome'])

    def test_late_negative_review_quarantines_exact_attempt_without_ordinary_retry(self):
        for verdict, reason in [('contradicted', 'summary_support_rejected'),
                                ('insufficient_context', 'summary_support_uncertain'),
                                ('malformed', 'summary_support_invalid')]:
            with self.subTest(verdict=verdict):
                self.data = self.data.parent / verdict
                configure(self.data, jev_filter_enabled=False, jev_quality_enabled=False)
                self.seed()
                def review(request):
                    with Store(self.data) as store:
                        store._connection.execute("UPDATE observation_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE status='running'")
                    result = rejected_review(request)
                    if verdict == 'malformed':
                        result['output']['units'] = {}
                    else:
                        result['output']['units']['completed']['comparisons'][0]['verdict'] = verdict
                    return result
                runner = mock.Mock(side_effect=self.generate)
                result = process_pending(self.project, self.data, runner=runner, summary_reviewer=review)
                self.assertEqual(reason, result.get('reason_code'), result)
                records, attempts, raw = self.stored(result)
                self.assertEqual([], records)
                self.assertIsNone(raw['superseded_by'])
                self.assertIn('summary_support_1', json.loads(attempts[0]['execution_calls']))
                again = process_pending(self.project, self.data, runner=runner, summary_reviewer=supported_review)
                self.assertEqual('idle', again['status'], again)
                runner.assert_called_once()

    def test_late_supported_review_cannot_commit_through_expired_lease(self):
        self.seed()
        def review(request):
            with Store(self.data) as store:
                store._connection.execute("UPDATE observation_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE status='running'")
            return supported_review(request)
        result = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=review)
        self.assertEqual('lease_expired', result['code'])
        self.assertEqual([], self.stored(result)[0])
        again = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=supported_review)
        self.assertEqual('processed', again['status'], again)

    def test_late_rejection_does_not_mutate_replacement_lease(self):
        self.seed()
        def review(request):
            with Store(self.data) as store:
                store._connection.execute("UPDATE observation_jobs SET lease_token='replacement', attempt_count=attempt_count+1 WHERE status='running'")
            return rejected_review(request)
        result = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=review)
        self.assertEqual([], self.stored(result)[0])
        with Store(self.data) as store:
            row = store._connection.execute('SELECT status,lease_token,attempt_count FROM observation_jobs WHERE id=?', (result['job_id'],)).fetchone()
        self.assertEqual(('running', 'replacement', 2), tuple(row))

    def test_service_continues_fresh_work_without_replaying_semantic_quarantine(self):
        from tests.test_service import Clock
        from codex_mem.service import enqueue, run_service, service_status
        from codex_mem.observer_usage_store import COUNTERS
        for verdict in ('contradicted', 'insufficient_context'):
            with self.subTest(verdict=verdict):
                self.data = self.data.parent / ('service-' + verdict)
                configure(self.data, capture_scope='all', service_enabled=True, semantic_enabled=False,
                          jev_filter_enabled=False, jev_quality_enabled=False)
                self.seed()
                bad_id = self.raw['id']
                clock = Clock()
                generated = []
                receipts = []
                def measured(result):
                    tokens = dict.fromkeys(COUNTERS, 0)
                    tokens.update(input_tokens=10, output_tokens=2, total_tokens=12)
                    result['metrics'] = dict(duration_ms=1, usage=dict(status='reported',
                        source='app_server_thread_total', updates=1, tokens=tokens))
                    return result
                def generate(request):
                    ids = [source['id'] for source in request['sources']]
                    titles = [source['title'] for source in request['sources']]
                    generated.append(titles)
                    if 'Answer' in titles:
                        return measured(self.generate(request))
                    return measured(receipt(dict(disposition='processed', session_summary=None,
                        notes=[dict(title='Later work', body=request['sources'][0]['body'],
                                    tags=[], source_ids=ids)])))
                def review(request):
                    result = rejected_review(request)
                    result['output']['units']['completed']['comparisons'][0]['verdict'] = verdict
                    return measured(result)
                def processor(project, **kwargs):
                    self.assertFalse(kwargs.get('retry_failed'))
                    result = process_pending(project, runner=generate, summary_reviewer=review, **kwargs)
                    receipts.append(result)
                    return result
                def add_later(session):
                    with Store(self.data) as store:
                        return store.remember(self.project, 'Later work', 'Preserve the request ID.',
                            source='hook:PostToolUse', session_id=session)
                first = add_later('later-one')
                enqueue(self.project, self.data, clock=clock)
                run_service(self.data, processor=processor, clock=clock,
                            sleeper=clock.sleep, max_cycles=4)
                self.assertEqual(['failed', 'processed', 'idle'], [r['status'] for r in receipts])
                failed_id = receipts[0]['job_id']
                with Store(self.data) as store:
                    failed_attempts = store.observation_attempts(self.project, failed_id)
                    self.assertEqual(24, failed_attempts[0]['total_tokens'])
                    self.assertEqual('failed', failed_attempts[0]['outcome'])
                    self.assertIsNotNone(store.get(self.project, [first['id']])[0]['superseded_by'])
                later = add_later('later-two')
                enqueue(self.project, self.data, clock=clock)
                run_service(self.data, processor=processor, clock=clock,
                            sleeper=clock.sleep, max_cycles=3)
                with Store(self.data) as store:
                    self.assertEqual(failed_attempts, store.observation_attempts(self.project, failed_id))
                    self.assertEqual('failed', store.observation_job_status(self.project, failed_id)['status'])
                    self.assertIsNone(store.get(self.project, [bad_id])[0]['superseded_by'])
                    self.assertIsNotNone(store.get(self.project, [later['id']])[0]['superseded_by'])
                self.assertEqual(1, sum('Answer' in titles for titles in generated))
                self.assertEqual(3, len(generated))
                status = service_status(self.data, clock=clock)
                self.assertEqual(0, status['blocked_projects'])
                self.assertEqual(1, status['quarantined_batches'])

    def test_repeated_unicode_and_punctuation_do_not_require_model_text_copying(self):
        self.seed(answer='Начало. Résultat 🙂 passed. Fin. Résultat 🙂 passed.')
        def review(request):
            evidence = request['evidence'][-1]
            self.assertEqual(evidence['body'], ''.join(span['text'] for span in evidence['spans']))
            return supported_review(request)
        result = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=review)
        self.assertEqual('processed', result['status'], result)

    def test_acknowledgement_stop_can_summarize_prior_reported_result(self):
        from tests.test_jev_quality import note
        with Store(self.data) as store:
            store.remember(self.project, 'Tool result', 'Local tests passed, 4 tests.',
                           source='hook:PostToolUse', session_id='session')
        def generate_note(request):
            return receipt(dict(disposition='processed', notes=[note(request['sources'][0]['id'],
                title='Local tests', body='Local tests passed, 4 tests.')], session_summary=None))
        first = process_pending(self.project, self.data, runner=generate_note)
        self.assertEqual('processed', first['status'], first)
        with Store(self.data) as store:
            self.raw = store.remember(self.project, 'Acknowledgement', 'Done.', source='hook:Stop', session_id='session')
        def generate_summary(request):
            result = self.generate(request)
            result['output']['session_summary'].update(title='Local test result', request=None,
                                                       completed='Earlier local tests passed, 4 tests.')
            return result
        def review(request):
            self.assertTrue(any(s['evidence_group'] == 'history' and s['evidence_role'] == 'derived_note'
                                for s in request['evidence']))
            evidence = next(s for s in request['evidence'] if s['evidence_group'] == 'history'
                            and 'Local tests passed, 4 tests.' in s['body'])
            result = supported_review(request)
            for unit in result['output']['units'].values():
                unit['comparisons'][0]['evidence_span_ids'] = [evidence['spans'][0]['id']]
            return result
        result = process_pending(self.project, self.data, runner=generate_summary, summary_reviewer=review)
        self.assertEqual('processed', result['status'], result)
        summary = self.stored(result)[0][0]['session_summary']
        self.assertIsNone(summary['request'])
        self.assertEqual('Earlier local tests passed, 4 tests.', summary['completed'])


class NativeSummarySupportTests(unittest.TestCase):
    def setUp(self):
        import os
        import sys
        self.os = os
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()
        self.data = self.root / 'memory'
        for runtime in ('codex', 'pi'):
            path = self.root / runtime
            path.write_text('#!' + sys.executable + '\n' + (Path(__file__).parent / f'fixtures/execution_{runtime}.py').read_text())
            path.chmod(0o700)
        environment = mock.patch.dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                                      PI_CODING_AGENT_DIR=str(self.root / 'native'))
        environment.start()
        self.addCleanup(environment.stop)

    def setup_case(self, runtime, **scenario):
        # Each subtest owns a new database and native scenario.
        self.data = self.root / ('memory-' + str(len(list(self.root.glob('memory-*')))))
        profile = dict(runtime=runtime, provider='fixture' if runtime == 'pi' else 'openai',
                       model='fixture' if runtime == 'pi' else MODEL,
                       reasoning_effort='low' if runtime == 'pi' else REASONING_EFFORT, timeout=4)
        configure(self.data, processing_profile=profile, jev_filter_enabled=False, jev_quality_enabled=False,
                  service_enabled=False, semantic_enabled=False)
        with Store(self.data) as store:
            self.raw = store.remember(self.project, 'Outcome', 'The assistant reported local import is requested.',
                                      source='hook:Stop', session_id='session')
        summary = dict(title='Import session', request=None, investigated='', learned='',
                       completed='The assistant reported local import is requested.', next_steps='', notes='', source_ids=['s1'])
        base = dict(observation=True) if runtime == 'pi' else dict(
            output=dict(result=dict(notes=[], disposition='processed', session_summary=summary)))
        (self.root / runtime).with_suffix('.json').write_text(json.dumps(dict(base, **scenario)))
        (self.root / runtime).with_suffix('.support-count').unlink(missing_ok=True)
        (self.root / runtime).with_suffix('.trace').unlink(missing_ok=True)
        return profile

    def attempt(self, result):
        with Store(self.data) as store:
            return store.observation_attempts(self.project, result['job_id'])[0]

    def test_both_runtime_fixtures_retain_complete_long_summary_fields(self):
        for runtime in ('codex', 'pi'):
            for length in (1201, 3000):
                with self.subTest(runtime=runtime, length=length):
                    text = ('The assistant reported local import is requested. ' * 70)[:length - 1] + '.'
                    self.assertEqual(length, len(text))
                    summary = dict(title='Import report', request=None, investigated='', learned='',
                        completed='', next_steps='', notes=text, source_ids=['s1'])
                    self.setup_case(runtime, output=dict(result=dict(notes=[], disposition='processed',
                                                                     session_summary=summary)))
                    result = process_pending(self.project, self.data, codex=str(self.root / 'codex'))
                    self.assertEqual('processed', result['status'], result)
                    with Store(self.data) as store:
                        job = store.observation_job_status(self.project, result['job_id'])
                        records = store.get(self.project, job['output_ids'])
                    self.assertEqual(1, len(records))
                    self.assertEqual('session_summary', records[0]['kind'])
                    self.assertEqual('Notes: ' + text, records[0]['body'])
                    calls = json.loads(self.attempt(result)['execution_calls'])
                    self.assertEqual({'generation', 'summary_support_1'}, set(calls))
                    self.assertEqual('supported', calls['summary_support_1']['review_diagnostic']['status'])

    def test_both_runtime_adapters_review_and_correct_with_same_profile(self):
        for runtime in ('codex', 'pi'):
            with self.subTest(runtime=runtime):
                profile = self.setup_case(runtime, support_modes=['contradicted', 'supported'])
                result = process_pending(self.project, self.data, codex=str(self.root / 'codex'))
                self.assertEqual('processed', result['status'], result)
                attempt = self.attempt(result)
                calls = json.loads(attempt['execution_calls'])
                self.assertEqual({'generation', 'summary_support_1', 'summary_support_2'}, set(calls))
                self.assertEqual(48, attempt['total_tokens'])
                for call in calls.values():
                    self.assertEqual(profile, call['requested_profile'])
                    self.assertEqual(runtime, call['actual_identity']['runtime'])
                self.assertEqual(24, calls['generation']['metrics']['total_tokens'])
                self.assertEqual(12, calls['summary_support_1']['metrics']['total_tokens'])
                self.assertEqual(12, calls['summary_support_2']['metrics']['total_tokens'])
                self.assertLessEqual(attempt['duration_ms'], sum(c['metrics']['duration_ms'] for c in calls.values()) + 150)

    def test_both_runtime_adapters_quarantine_bad_review_and_preserve_usage(self):
        for runtime in ('codex', 'pi'):
            for mode, reason in [('malformed_review', 'summary_support_invalid'),
                                 ('invalid_json', 'summary_support_invalid'),
                                 ('duplicate_units', 'summary_support_invalid'),
                                 ('insufficient_context', 'summary_support_uncertain'),
                                 ('tool', 'summary_support_unavailable'),
                                 ('rerouted', 'summary_support_unavailable'),
                                 ('timeout', 'summary_support_unavailable')]:
                with self.subTest(runtime=runtime, mode=mode):
                    self.setup_case(runtime, support_mode=mode)
                    result = process_pending(self.project, self.data, codex=str(self.root / 'codex'))
                    self.assertEqual('failed', result['status'], result)
                    self.assertEqual(reason, result.get('reason_code'), result)
                    with Store(self.data) as store:
                        job = store.observation_job_status(self.project, result['job_id'])
                        self.assertEqual([], job['output_ids'])
                        self.assertIsNone(store.get(self.project, [self.raw['id']])[0]['superseded_by'])
                    attempt = self.attempt(result)
                    calls = json.loads(attempt['execution_calls'])
                    self.assertGreaterEqual(attempt['total_tokens'], 24)
                    self.assertEqual(12, calls['summary_support_1']['metrics']['total_tokens'])

    def test_correction_cannot_discard_previously_generated_summary(self):
        for runtime in ('codex', 'pi'):
            with self.subTest(runtime=runtime):
                self.setup_case(runtime, support_mode='contradicted')
                scenario_path = (self.root / runtime).with_suffix('.json')
                scenario = json.loads(scenario_path.read_text())
                summary = dict(title='Import session', request=None, investigated='', learned='',
                    completed='The assistant reported local import is requested.', next_steps='', notes='', source_ids=['s1'])
                scenario['outputs'] = [dict(result=dict(notes=[], disposition='processed', session_summary=summary)),
                                       dict(result=dict(notes=[], disposition='skipped', session_summary=None))]
                scenario_path.write_text(json.dumps(scenario))
                result = process_pending(self.project, self.data, codex=str(self.root / 'codex'))
                self.assertEqual('summary_support_rejected', result.get('reason_code'), result)
                with Store(self.data) as store:
                    job = store.observation_job_status(self.project, result['job_id'])
                    self.assertEqual('failed', job['status'])
                    self.assertEqual([], job['output_ids'])
                    self.assertIsNone(store.get(self.project, [self.raw['id']])[0]['superseded_by'])

    def test_optional_quality_and_summary_share_one_correction(self):
        from tests.test_jev_quality import response
        for runtime in ('codex', 'pi'):
            with self.subTest(runtime=runtime):
                self.setup_case(runtime, support_modes=['supported', 'contradicted'])
                configure(self.data, jev_quality_enabled=True)
                result = process_pending(self.project, self.data, codex=str(self.root / 'codex'),
                    jev_quality_evaluator=lambda payload: response(payload, .5, .1))
                self.assertEqual('summary_support_rejected', result.get('reason_code'), result)
                calls = json.loads(self.attempt(result)['execution_calls'])
                self.assertEqual(3, len(calls))
                self.assertEqual(24, calls['generation']['metrics']['total_tokens'])
