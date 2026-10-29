"""Offline public pipeline coverage for complete, bounded outcome review."""
import copy
import json
import unittest

from codex_mem.processor import process_pending
from codex_mem.store import Store
from codex_mem.summary_outcome import SOURCE_VERSION, OUTCOME_VERSION
from codex_mem.summary_support import VERSION
from tests import test_summary_support as fixtures
from tests.test_summary_support import receipt, supported_review, source_output, outcome_output


class OutcomeChunksTests(unittest.TestCase):
    setUp = fixtures.SummarySupportTests.setUp
    seed = fixtures.SummarySupportTests.seed
    generate = fixtures.SummarySupportTests.generate
    stored = fixtures.SummarySupportTests.stored

    def review(self, request):
        if request['version'] == SOURCE_VERSION:
            return receipt(source_output(propositions=[dict(statement=f'Reported check {i} passed.',
                subject=f'Check {i}', temporal_scope='Current report', modality='reported',
                source_id='report1') for i in range(8)]))
        response = supported_review(request)
        if request['version'] == VERSION:
            row = response['output']['units']['completed'][0]
            response['output']['units']['completed'] = [dict(copy.deepcopy(row),
                candidate_assertion=f'The report describes check {i}.') for i in range(6)]
        return response

    def test_every_relation_is_reviewed_before_summary_is_stored(self):
        self.seed(intent='Report the local checks.', answer='Six local checks passed; two checks remain.')
        requests = []
        def review(request):
            if request['version'] == OUTCOME_VERSION:
                requests.append(copy.deepcopy(request))
                with Store(self.data) as store:
                    self.assertFalse(any(r['kind']=='session_summary' for r in store.timeline(self.project)))
            return self.review(request)
        result = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=review)
        self.assertEqual('processed', result['status'], result)
        self.assertEqual([50,20], [len(r['relations']) for r in requests])
        self.assertEqual([f'r{i}' for i in range(1,71)], [p['id'] for r in requests for p in r['relations']])
        self.assertEqual(70, len({(p['claim_id'],p['source_item_id']) for r in requests for p in r['relations']}))
        self.assertTrue(self.stored(result)[0])
        with Store(self.data) as store:
            calls = json.loads(store.observation_attempts(self.project,result['job_id'])[0]['execution_calls'])
        for suffix in (1,2):
            self.assertEqual(OUTCOME_VERSION, calls[f'summary_outcome_1_chunk_{suffix}']['review_diagnostic']['version'])

    def test_late_group_rejection_or_invalid_output_cannot_write(self):
        from codex_mem.config import configure
        for state, reason in (('incompatible','summary_support_rejected'),
                              ('unresolved','summary_support_uncertain'),
                              ('missing','summary_review_malformed'),
                              ('duplicate','summary_review_malformed')):
            with self.subTest(state=state):
                self.data = self.data.parent / state
                configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                          jev_filter_enabled=False, jev_quality_enabled=False)
                self.seed()
                generations, groups = [], []
                def generate(request):
                    generations.append(request)
                    return self.generate(request)
                def review(request):
                    response = self.review(request)
                    if request['version'] == OUTCOME_VERSION:
                        groups.append(request['relations'][0]['id'])
                        if request['relations'][0]['id'] == 'r51':
                            rows = response['output']['rows']
                            if state == 'missing':
                                rows.pop()
                            elif state == 'duplicate':
                                rows[-1] = dict(rows[0])
                            else:
                                rows[0].update(applicability='present', scope_relation='aligned', value=state)
                    return response
                result = process_pending(self.project,self.data,runner=generate,summary_reviewer=review)
                self.assertEqual(reason,result.get('reason_code'),result)
                self.assertEqual(['r1','r51'],groups)
                self.assertEqual(1,len(generations))
                self.assertFalse(self.stored(result)[0])

    def test_source_or_lease_change_between_groups_stops_dispatch(self):
        from codex_mem.config import configure
        for change in ('source','lease'):
            with self.subTest(change=change):
                self.data = self.data.parent / change
                configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                          jev_filter_enabled=False, jev_quality_enabled=False)
                self.seed()
                groups = []
                def review(request):
                    if request['version'] == OUTCOME_VERSION:
                        groups.append(request['relations'][0]['id'])
                        with Store(self.data) as store:
                            if change == 'source':
                                store.forget(self.project,[self.raw['id']])
                            else:
                                store._connection.execute("UPDATE observation_jobs SET lease_expires_at='2000-01-01T00:00:00Z' WHERE status='running'")
                    return self.review(request)
                result = process_pending(self.project,self.data,runner=self.generate,summary_reviewer=review)
                self.assertNotEqual('processed',result['status'],result)
                self.assertEqual(['r1'],groups)
                with Store(self.data) as store:
                    self.assertEqual([],store.observation_job_status(self.project,result['job_id'])['output_ids'])

    def test_each_group_has_its_own_usage_with_shared_deadline_and_profile(self):
        from codex_mem.execution import execute_codex
        from codex_mem.observer_usage_store import COUNTERS
        self.seed()
        requests = []
        def measured(request):
            response = self.review(request)
            count = 11 if request.get('relations',[{}])[0].get('id') == 'r1' else 17
            counters = dict.fromkeys(COUNTERS,0)
            counters.update(input_tokens=count-2,output_tokens=2,total_tokens=count)
            response['metrics'] = dict(duration_ms=1,usage=dict(status='reported',
                source='app_server_thread_total',updates=1,tokens=counters))
            return response
        class Reviewer:
            def execute(inner, request):
                requests.append(request)
                return execute_codex(request,measured)
        result = process_pending(self.project,self.data,runner=self.generate,summary_reviewer=Reviewer())
        self.assertEqual('processed',result['status'],result)
        self.assertEqual(1,len({r.deadline for r in requests}))
        self.assertEqual(1,len({r.profile for r in requests}))
        self.assertEqual(240,requests[0].profile.timeout)
        with Store(self.data) as store:
            calls = json.loads(store.observation_attempts(self.project,result['job_id'])[0]['execution_calls'])
        self.assertEqual(11,calls['summary_outcome_1_chunk_1']['metrics']['total_tokens'])
        self.assertEqual(17,calls['summary_outcome_1_chunk_2']['metrics']['total_tokens'])

    def test_shared_deadline_exhaustion_does_not_start_another_group(self):
        import time
        from unittest.mock import patch
        from codex_mem.execution import execute_codex
        self.seed()
        now = time.monotonic()
        clock = [now]
        groups = []
        class Reviewer:
            def execute(inner, request):
                response = execute_codex(request,self.review)
                if request.context['version'] == OUTCOME_VERSION:
                    groups.append(request.context['relations'][0]['id'])
                    clock[0] = request.deadline + 1
                return response
        with patch('codex_mem.processor.time.monotonic',side_effect=lambda: clock[0]):
            result = process_pending(self.project,self.data,runner=self.generate,summary_reviewer=Reviewer())
        self.assertEqual('summary_support_unavailable',result.get('reason_code'),result)
        self.assertEqual(['r1'],groups)
        self.assertFalse(self.stored(result)[0])

    def test_multiple_group_conflicts_share_one_generation_correction(self):
        from unittest.mock import patch
        from codex_mem.processor import NativeProcessorRunner
        self.seed()
        groups, feedback = [], []
        def review(request):
            response = self.review(request)
            if request['version'] == OUTCOME_VERSION:
                groups.append(request['relations'][0]['id'])
                response['output'] = outcome_output(request,'incompatible')
            return response
        def generate(instance, request):
            candidate = self.generate(request)['output']
            feedback.append(instance.candidate_review(candidate,240))
            candidate['session_summary']['completed'] = 'The report describes a different partial result.'
            instance.candidate_review(candidate,240)
            self.fail('Second rejected candidate must not receive another correction')
        with patch.object(NativeProcessorRunner,'check_profile'), patch.object(NativeProcessorRunner,'run',generate):
            result = process_pending(self.project,self.data,summary_reviewer=review)
        self.assertEqual('summary_support_rejected',result.get('reason_code'),result)
        self.assertEqual(['r1','r51','r1','r51'],groups)
        self.assertEqual(1,len(feedback))
        self.assertIn('r1',feedback[0])
        self.assertIn('r58',feedback[0])
        self.assertFalse(self.stored(result)[0])

    def test_service_uses_the_final_chunk_receipt_and_rejects_incomplete_proof(self):
        from codex_mem.config import configure
        from codex_mem.service import enqueue, run_service, service_status
        for mutation, accepted in (('none',True),('later_pass',False),('later_failed',False),
                                   ('gap',False),('mixed_legacy',False),('earlier_rejected',False),
                                   ('old_chunk_version',False)):
            with self.subTest(mutation=mutation):
                self.data = self.data.parent / ('proof-' + mutation)
                configure(self.data, capture_scope='all', service_enabled=True, semantic_enabled=False,
                          jev_filter_enabled=False, jev_quality_enabled=False)
                self.seed()
                def review(request):
                    response = self.review(request)
                    if request['version'] == OUTCOME_VERSION and request['relations'][0]['id'] == 'r51':
                        response['output']['rows'][0].update(applicability='absent',scope_relation='aligned',value='compatible')
                    return response
                failed = process_pending(self.project,self.data,runner=self.generate,summary_reviewer=review)
                self.assertEqual('summary_review_malformed',failed.get('reason_code'),failed)
                with Store(self.data) as store:
                    calls = json.loads(store.observation_attempts(self.project,failed['job_id'])[0]['execution_calls'])
                    first, last = calls['summary_outcome_1_chunk_1'],calls['summary_outcome_1_chunk_2']
                    if mutation == 'later_pass':
                        first['review_diagnostic'] = copy.deepcopy(last['review_diagnostic'])
                        last['review_diagnostic'].update(status='unused',invalid_subtype=None)
                        last['review_diagnostic'].pop('rule_code',None)
                        last['review_diagnostic'].pop('unit_id',None)
                    elif mutation == 'later_failed': last['outcome'] = 'failed'
                    elif mutation == 'gap': calls.pop('summary_outcome_1_chunk_1')
                    elif mutation == 'mixed_legacy':
                        calls['summary_outcome_1'] = dict(last,call_id='summary_outcome_1')
                    elif mutation == 'earlier_rejected': first['review_diagnostic']['status'] = 'rejected'
                    elif mutation == 'old_chunk_version':
                        first['review_diagnostic']['version'] = 'summary-outcome-v5'
                        last['review_diagnostic']['version'] = 'summary-outcome-v5'
                    store._connection.execute('UPDATE observer_usage_attempts SET execution_calls=? WHERE job_id=?',
                                              (json.dumps(calls),failed['job_id']))
                enqueue(self.project,self.data,clock=lambda:1000)
                result = run_service(self.data,processor=lambda *_a,**_kw: failed,processor_workers=1,
                                     clock=lambda:1000,sleeper=lambda _:None,max_cycles=1)
                self.assertEqual(accepted,result['status'] != 'halted',result)
                if accepted:
                    self.assertEqual(1,service_status(self.data,clock=lambda:1000)['quarantined_batches'])
                with Store(self.data) as store:
                    self.assertEqual([],store.observation_job_status(self.project,failed['job_id'])['output_ids'])
                    self.assertEqual(calls,json.loads(store.observation_attempts(self.project,failed['job_id'])[0]['execution_calls']))
