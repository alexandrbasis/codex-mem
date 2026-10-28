"""Synthetic typed contracts test policy and ownership, never model quality."""
import copy
import dataclasses
import unittest
from codex_mem.summary_support import VERSION as SUPPORT_VERSION
from tests.test_summary_support import support_rows
from tests.support_contract_fixtures import authored_support_reply

from codex_mem import summary_support as support


class StructuralSupportTests(unittest.TestCase):
    def setUp(self):
        self.summary = dict(title='Report', completed='The report used a list; final external result unknown.')
        self.claim = dict(sources=[dict(id='raw', source='hook:Stop', body='A supplied report.')], context=[])
        self.prompt, self.schema, self.payload = support.prepare(self.summary, self.claim)

    def output(self):
        return dict(version=SUPPORT_VERSION, units={u:[dict(unit_id=u, coverage='complete',
            candidate_assertion='Synthetic scoped report assertion.', evidence_assertion='Supplied report.',
            value_comparison='Same value.', attribution_comparison='Same speaker.', scope_comparison='Same scope.',
            verdict='supported', citations=[dict(span_id='current1:1', role='report_anchor')],
            candidate_span_ids=[self.payload['candidate_spans'][u][0]['id']],
            report_binding=dict(target='report_text', properties=['content']))]
            for u in self.payload['units']})

    def test_original_report_bindings_produce_immutable_owned_claims(self):
        self.assertEqual(SUPPORT_VERSION, self.payload['version'])
        review = support.validate(self.output(), self.payload)
        self.assertEqual((), review.issues)
        self.assertEqual(('current1',), review.claims[0].report_ids)
        self.assertEqual('raw', self.payload['report_catalog']['current1']['record_id'])
        self.assertEqual(self.summary['completed'], self.payload['units']['completed'])
        self.assertRegex(review.candidate_fingerprint, r'^[0-9a-f]{64}$')
        with self.assertRaises(dataclasses.FrozenInstanceError): review.claims[0].target = 'other'


class StructuralSourceTests(unittest.TestCase):
    def test_body_only_source_preserves_unavailable_slots_and_actual_report_identity(self):
        from codex_mem import summary_outcome as outcome
        claim = dict(sources=[dict(id='derived', source='hook:Stop', kind='session_summary', body='PRIVATE_DERIVED'),
            dict(id='raw', source='hook:Stop', body='Original report body.')], context=[])
        prompt, schema, context = outcome.prepare_source(claim)
        self.assertEqual('summary-source-v4', context['version'])
        self.assertNotIn('PRIVATE_DERIVED', prompt)
        self.assertNotIn('current2', prompt)
        output = dict(version='summary-source-v4', coverage='partial', propositions=[], text_observations={
            'language':dict(status='unknown', value=None, scope='This report', reason='Unavailable language'),
            'organization':dict(status='not_applicable', value=None, scope='This report', reason='No applicable organization')})
        normalized = outcome.validate_source(output, context)
        self.assertEqual('raw', normalized['report']['provenance']['id'])
        self.assertEqual('current2', normalized['report']['catalog_id'])
        self.assertEqual(['unknown','not_applicable'], [i['availability'] for i in normalized['items']])
        self.assertEqual(['Unavailable language','No applicable organization'], [i['reason'] for i in normalized['items']])
        self.assertTrue(all(i['target']=='report_text' for i in normalized['items']))
        claim['sources'] = claim['sources'][:1]
        self.assertIsNone(outcome.prepare_source(claim))


class StructuralOutcomeTests(unittest.TestCase):
    def setUp(self):
        from codex_mem import summary_outcome as outcome
        self.outcome = outcome
        self.summary = dict(title='Report', completed='The report used a list; final external result unknown.')
        self.claim = dict(sources=[dict(id='raw', source='hook:Stop', body='A supplied report.')], context=[])
        _, _, self.payload = support.prepare(self.summary, self.claim)
        output = StructuralSupportTests.output(self)
        support_rows(output)[0]['report_binding'] = dict(target='other', properties=[])
        support_rows(output)[0]['citations']=[dict(ref,role='evidence') for ref in support_rows(output)[0]['citations']]
        support_rows(output)[1]['report_binding']['properties'] = ['language']
        self.review = support.validate(output, self.payload)
        _, _, source = outcome.prepare_source(self.claim)
        self.normalized = outcome.validate_source(dict(version='summary-source-v4', coverage='partial', propositions=[],
            text_observations={'language':dict(status='observed', value='Observed source language', scope='This report', reason=None),
                'organization':dict(status='unknown', value=None, scope='This report', reason='Unavailable organization')}), source)

    def prepared(self):
        return self.outcome.prepare_outcome(self.summary, self.review, self.normalized, self.payload)

    def typed_output(self, payload, app='present', scope='aligned', value='compatible'):
        # This authored scenario compares the first item; all other sent properties are explicitly unused.
        item_id=payload['interpretation']['items'][0]['id']
        return dict(version='summary-outcome-v6', rows=[dict(relation_id=pair['id'],
            applicability=app if pair['source_item_id']==item_id else 'absent',
            scope_relation=scope if pair['source_item_id']==item_id else 'not_compared',
            value=value if pair['source_item_id']==item_id else 'not_compared',
            scope_comparison='Synthetic scope comparison.', value_comparison='Synthetic value comparison.')
            for pair in payload['relations']])

    def test_applicable_unresolved_blocks_but_unused_does_not(self):
        _, _, payload = self.prepared()
        self.assertEqual(2, len(payload['relations']))
        unresolved = self.outcome.validate_outcome(self.typed_output(payload,value='unresolved'), payload)
        self.assertEqual('uncertain', unresolved.status)
        self.assertEqual('insufficient_context', unresolved.issues[0]['verdict'])
        unused = self.outcome.validate_outcome(self.typed_output(payload,'absent','not_compared','not_compared'), payload)
        self.assertEqual('unused', unused.status)
        self.assertEqual((), unused.issues)



class StructuralPublicTests(unittest.TestCase):
    from tests import test_summary_support as fixtures
    setUp = fixtures.SummarySupportTests.setUp
    seed = fixtures.SummarySupportTests.seed
    generate = fixtures.SummarySupportTests.generate
    stored = fixtures.SummarySupportTests.stored

    def test_applicable_uncertainty_atomically_blocks_summary_and_note(self):
        from codex_mem.processor import process_pending
        from tests.test_summary_support import supported_review, receipt
        self.seed()
        seen = []
        def generate(request):
            result = self.generate(request)
            result['output']['notes'] = [dict(title='Supported note', body='Synthetic supported note.',
                tags=[], source_ids=[request['sources'][-1]['id']])]
            return result
        def review(request):
            seen.append(request['version'])
            result = supported_review(request)
            if request['version'] == 'summary-outcome-v6':
                result['output']['rows'][0].update(applicability='present', scope_relation='aligned', value='unresolved')
            return result
        result = process_pending(self.project, self.data, runner=generate, summary_reviewer=review)
        self.assertEqual('summary_support_uncertain', result.get('reason_code'), result)
        self.assertEqual([], self.stored(result)[0])
        self.assertEqual([SUPPORT_VERSION,'summary-source-v4','summary-outcome-v6'], seen)

    def test_unclaimed_unknown_property_uses_existing_outcome_call_and_unused_ledger(self):
        import json
        from codex_mem.processor import process_pending
        from tests.test_summary_support import supported_review, receipt, source_output
        self.seed()
        seen = []
        def review(request):
            seen.append(request['version'])
            if request['version'] == 'summary-source-v4':
                return receipt(source_output(propositions=[]))
            return supported_review(request)
        result = process_pending(self.project, self.data, runner=self.generate, summary_reviewer=review)
        self.assertEqual('processed', result['status'], result)
        self.assertEqual([SUPPORT_VERSION,'summary-source-v4','summary-outcome-v6'], seen)
        self.assertEqual('unused', result['summary_support'][-1]['status'])
        calls = json.loads(self.stored(result)[1][0]['execution_calls'])
        self.assertEqual({'generation','summary_support_1','summary_source_1','summary_outcome_1'}, set(calls))


class StructuralMatrixTests(unittest.TestCase):
    setUp = StructuralOutcomeTests.setUp
    prepared = StructuralOutcomeTests.prepared
    typed_output = StructuralOutcomeTests.typed_output

    def test_exact_120_typed_states_and_unavailable_conflict_rejection(self):
        import itertools
        expected = {
            ('absent','not_compared','not_compared',False):'unused',
            ('absent','not_compared','not_compared',True):'unused',
            ('unresolved','unresolved','unresolved',False):'uncertain',
            ('unresolved','unresolved','unresolved',True):'uncertain',
            ('present','disjoint','not_compared',False):'unused',
            ('present','disjoint','not_compared',True):'unused',
            ('present','unresolved','unresolved',False):'uncertain',
            ('present','unresolved','unresolved',True):'uncertain',
            ('present','aligned','compatible',True):'checked',
            ('present','aligned','incompatible',True):'contradicted',
            ('present','aligned','unresolved',True):'uncertain',
            ('present','aligned','not_established',False):'uncertain',
        }
        count = 0
        for app, scope, value, available in itertools.product(
                ['absent','present','unresolved'], ['aligned','disjoint','unresolved','not_compared'],
                ['compatible','incompatible','unresolved','not_established','not_compared'], [False,True]):
            _, _, payload = self.prepared()
            payload['interpretation']['items'][0]['availability'] = 'observed' if available else 'unknown'
            output = self.typed_output(payload, app, scope, value)
            key = app, scope, value, available
            with self.subTest(state=key):
                if key in expected:
                    self.assertEqual(expected[key], self.outcome.validate_outcome(output,payload).status)
                else:
                    with self.assertRaises(support.SupportError) as caught: self.outcome.validate_outcome(output,payload)
                    self.assertEqual('verdict', caught.exception.subtype)
            count += 1
        self.assertEqual(120,count)

    def test_mixed_claim_decisions_preserve_contradiction_priority(self):
        output = StructuralSupportTests.output(self)
        support_rows(output)[0]['report_binding'] = dict(target='other',properties=[])
        support_rows(output)[0]['citations']=[dict(ref,role='evidence') for ref in support_rows(output)[0]['citations']]
        base = output['units']['completed'][0]
        base['report_binding']['properties'] = ['language']
        output['units']['completed'].extend([copy.deepcopy(base),copy.deepcopy(base)])
        self.review = support.validate(output,self.payload)
        _, _, payload = self.prepared()
        self.assertEqual(6,len(payload['relations']))
        result = self.typed_output(payload)
        result['rows'][0]['value']='incompatible'
        result['rows'][1].update(applicability='absent',scope_relation='not_compared',value='not_compared')
        result['rows'][2]['value']='unresolved'
        review = self.outcome.validate_outcome(result,payload)
        self.assertEqual('contradicted',review.status)
        self.assertEqual(['contradicted','insufficient_context'],[i['verdict'] for i in review.issues])
        self.assertEqual(['contradicted','unused','uncertain','unused','unused','unused'],
                         [d['decision'] for d in review.decisions if 'claim_id' in d])

    def test_qualifiers_parent_fields_and_fingerprints_cannot_be_replaced(self):
        _, _, payload = self.prepared()
        self.assertEqual(self.summary['completed'], payload['units']['completed'])
        self.assertEqual('exact',payload['relations'][0]['report_link'])
        for mutate in (
            lambda:self.summary.update(completed='Changed claim without its qualifier.'),
            lambda:self.payload['evidence'][0].update(body='Changed source'),
            lambda:self.payload['candidate_spans']['completed'][0].update(text='Borrowed field value'),
            lambda:self.payload['report_catalog']['current1'].update(record_id='foreign-record'),
            lambda:self.normalized['report'].update(catalog_id='history1'),
            lambda:self.normalized['report']['provenance'].update(id='foreign-record'),
        ):
            self.setUp();mutate()
            with self.assertRaises(support.SupportError):self.prepared()

    def test_relation_ids_exact_coverage_and_shapes_are_strict(self):
        _, _, payload = self.prepared()
        for change in (
            lambda o:o.update(version='summary-outcome-v3'), lambda o:o.update(rows=[]),
            lambda o:o['rows'].append(copy.deepcopy(o['rows'][0])),
            lambda o:o['rows'][0].update(relation_id='foreign'),
            lambda o:o['rows'][0].update(claim_id='foreign'),
            lambda o:o['rows'][0].update(value_comparison=' '),
            lambda o:o['rows'][0].update(scope_comparison='x'*601),
            lambda o:o['rows'][0].update(relation='no_conflict'),
        ):
            result = self.typed_output(payload);change(result)
            with self.assertRaises(support.SupportError):self.outcome.validate_outcome(result,payload)

    def test_fifty_independent_comparisons_fit_and_four_hundred_fail_before_dispatch(self):
        from tests.test_summary_support import supported_review, source_output
        self.summary={field:'A complete claim; retained qualifier.' for field in self.outcome.OUTCOME_FIELDS}
        _, _, self.payload=support.prepare(self.summary,self.claim)
        _, _, source=self.outcome.prepare_source(self.claim)
        output=source_output(propositions=[dict(statement=f'Content item {i}',subject='Report',temporal_scope='This report',
            modality='observed_text',source_id='report1',target='report_text') for i in range(8)],
            text_observations={key:dict(status='observed',value='Observed property',scope='This report',reason=None)
                for key in ('language','organization')})
        self.normalized=self.outcome.validate_source(output,source)
        support_output=supported_review(self.payload)['output'];self.review=support.validate(support_output,self.payload)
        _, schema, payload=self.prepared()
        self.assertEqual(50,len(payload['relations']))
        self.assertEqual(50,schema['properties']['rows']['maxItems'])
        support_output['units']={unit:[copy.deepcopy(row) for row in rows for _ in range(8)]
            for unit,rows in support_output['units'].items()}
        self.review=support.validate(support_output,self.payload)
        self.assertEqual(40,len(self.review.claims))
        with self.assertRaises(support.SupportError) as caught:self.prepared()
        self.assertEqual('bounds',caught.exception.subtype)

    def test_truthful_report_limit_and_recorded_plan_differ_from_unverified_external_outcomes(self):
        from tests.test_summary_support import supported_review, source_output
        for target, modality, assertion, statement, value, status in (
            ('report_text','observed_text','The report supplies no final status.','No final status appears in this report.','compatible','checked'),
            ('reported_content','unknown','The operation completed.','Actual outcome unknown.','not_established','uncertain'),
            ('report_text','observed_text','The report records a plan.','This report includes a proposed operation.','compatible','checked'),
            ('reported_content','planned','The report proposed the operation.','The operation is planned.','compatible','checked'),
            ('reported_content','planned','The report proposed a different operation.','The operation is planned.','incompatible','contradicted'),
            ('reported_content','planned','The operation completed.','The operation is planned.','unresolved','uncertain'),
        ):
            self.summary={'completed':assertion};_, _, self.payload=support.prepare(self.summary,self.claim)
            output=supported_review(self.payload)['output'];binding=support_rows(output)[0]['report_binding']
            binding.update(target=target,properties=['content']);self.review=support.validate(output,self.payload)
            _, _, source=self.outcome.prepare_source(self.claim)
            self.normalized=self.outcome.validate_source(source_output(propositions=[dict(statement=statement,subject='Operation/report',
                temporal_scope='This report',modality=modality,source_id='report1',target=target)]),source)
            _, _, payload=self.prepared()
            self.assertEqual(3,len(payload['relations']))
            self.assertEqual(status,self.outcome.validate_outcome(self.typed_output(payload,value=value),payload).status)


class StructuralBindingTests(unittest.TestCase):
    setUp = StructuralSupportTests.setUp
    output = StructuralSupportTests.output

    def test_catalog_uses_original_custody_and_does_not_reassign_earlier_report_to_acknowledgement(self):
        from codex_mem import summary_outcome as outcome
        from tests.test_summary_support import source_output
        self.claim=dict(sources=[dict(id='current',source='hook:Stop',body='Acknowledged.'),
            dict(id='lifecycle',source='hook:Stop',body='Lifecycle only.',_jev_lifecycle_only=True)],
            context=[dict(id='old',source='hook:Stop',body='Earlier report.'),
                dict(id='derived',source='hook:Stop',kind='session_summary',body='Derived summary.')])
        _, _, self.payload=support.prepare(self.summary,self.claim)
        self.assertEqual({'current1','history1'},set(self.payload['report_catalog']))
        output=self.output()
        for row in support_rows(output):
            row['citations']=[dict(span_id='history1:1', role='report_anchor')]
        review=support.validate(output,self.payload)
        _, _, source=outcome.prepare_source(self.claim)
        normalized=outcome.validate_source(source_output(),source)
        _, _, payload=outcome.prepare_outcome(self.summary,review,normalized,self.payload)
        self.assertEqual('current1',normalized['report']['catalog_id'])
        self.assertEqual([],payload['relations'])
        # Structurally valid bindings are not proof that the model understood the referent.
        for row in support_rows(output):
            row['citations']=[dict(span_id=ref, role='report_anchor') for ref in ['current1:1','history1:1']]
        review=support.validate(output,self.payload)
        normalized['items'].append(dict(id='p1',property='content',target='report_text',availability='observed_text',
            statement='Current acknowledgement text.',subject='Report',temporal_scope='Now',modality='observed_text',source_id='report1'))
        _, _, payload=outcome.prepare_outcome(self.summary,review,normalized,self.payload)
        self.assertEqual({'includes_other_reports'},{r['report_link'] for r in payload['relations']})

    def test_binding_ownership_scope_bypass_duplicates_and_excluded_units_fail_closed(self):
        for change in (
            lambda o:o['units']['completed'][0].update(candidate_span_ids=['title:1']),
            lambda o:o['units']['completed'][0].update(candidate_span_ids=[]),
            lambda o:o['units']['completed'][0].update(candidate_span_ids=['completed:1','completed:1']),
            lambda o:o['units']['completed'][0].update(citations=[dict(span_id='history999:1',role='report_anchor')]),
            lambda o:o['units']['completed'][0].update(citations=[]),
            lambda o:o['units']['completed'][0]['report_binding'].update(properties=['language','language']),
            lambda o:o['units']['completed'][0]['report_binding'].update(target='reported_content',properties=['language']),
            lambda o:o['units']['completed'][0]['report_binding'].update(target='outside_outcome',properties=[]),
            lambda o:o['units']['completed'][0]['report_binding'].update(target='other'),
            lambda o:o['units']['completed'][0]['report_binding'].update(target='unresolved'),
            lambda o:o['units']['completed'][0]['report_binding'].update(record_id='forged'),
        ):
            output=self.output();change(output)
            with self.assertRaises(support.SupportError):support.validate(output,self.payload)
        from tests.test_summary_support import supported_review
        self.summary['request']='Original request'
        _, _, self.payload=support.prepare(self.summary,self.claim)
        output=supported_review(self.payload)['output']
        row=next(r for r in support_rows(output) if r['unit_id']=='request')
        row['report_binding'].update(target='report_text',properties=['content'])
        row['citations']=[dict(span_id='current1:1',role='report_anchor')]
        with self.assertRaises(support.SupportError):support.validate(output,self.payload)

    def test_unresolved_binding_is_uncertain_while_adverse_support_verdict_is_preserved(self):
        output=self.output()
        support_rows(output)[1]['report_binding']=dict(target='unresolved',properties=[])
        support_rows(output)[1]['citations']=[dict(ref,role='evidence') for ref in support_rows(output)[1]['citations']]
        review=support.validate(output,self.payload)
        self.assertEqual(['insufficient_context'],[i['verdict'] for i in review.issues])
        output['units']['completed'][0]['verdict']='contradicted'
        review=support.validate(output,self.payload)
        self.assertEqual(['contradicted','insufficient_context'],[i['verdict'] for i in review.issues])


class StructuralPublicBudgetTests(unittest.TestCase):
    setUp = StructuralPublicTests.setUp
    seed = StructuralPublicTests.seed
    generate = StructuralPublicTests.generate
    stored = StructuralPublicTests.stored

    def test_overflow_fails_before_outcome_dispatch_without_silent_pruning(self):
        from codex_mem.processor import process_pending
        from tests.test_summary_support import supported_review, source_output, receipt
        self.seed()
        seen=[]
        def generate(request):
            result=self.generate(request)
            for field in ('title','investigated','learned','completed','notes'):
                result['output']['session_summary'][field]='A complete synthetic claim; all qualifiers retained.'
            return result
        def review(request):
            seen.append(request['version'])
            if request['version']=='summary-source-v4':
                return receipt(source_output(propositions=[dict(statement=f'Observed report property {i}',subject='Report',
                    temporal_scope='This report',modality='observed_text',source_id='report1',target='report_text') for i in range(8)],
                    text_observations={key:dict(status='observed',value='Observed property',scope='This report',reason=None)
                        for key in ('language','organization')}))
            result=supported_review(request)
            result['output']['units']={unit:[copy.deepcopy(row) for row in rows
                for _ in range(8 if unit in request['candidate_spans'] else 1)]
                for unit,rows in result['output']['units'].items()}
            return result
        result=process_pending(self.project,self.data,runner=generate,summary_reviewer=review)
        self.assertEqual('summary_support_invalid',result.get('reason_code'),result)
        self.assertEqual('bounds',result['summary_support'][-1]['invalid_subtype'])
        self.assertEqual([SUPPORT_VERSION,'summary-source-v4'],seen)
        self.assertEqual([],self.stored(result)[0])

    def test_correction_uses_one_profile_and_deadline_with_at_most_seven_executions(self):
        import json
        from unittest import mock
        from codex_mem.processor import process_pending, NativeProcessorRunner
        from codex_mem.execution import execute_codex
        from tests.test_summary_support import supported_review, outcome_output, receipt
        self.seed();seen=[];generation_deadlines=[];test=self
        class Reviewer:
            def execute(self,request):
                seen.append(request)
                def review(context):
                    result=supported_review(context)
                    if context['version']=='summary-outcome-v6':
                        result['output']=outcome_output(context,'conflict' if sum(r.context['version']=='summary-outcome-v6' for r in seen)==1 else 'compatible')
                    return result
                return execute_codex(request,review)
        def generate(instance,request):
            generation_deadlines.append(request['deadline'])
            instance.before_dispatch()
            candidate=test.generate(request)['output']
            test.assertIsInstance(instance.candidate_review(candidate,240),str)
            candidate['session_summary']['completed']='A changed, still attributed synthetic result.'
            test.assertIsNone(instance.candidate_review(candidate,240))
            return receipt(candidate)
        with mock.patch.object(NativeProcessorRunner,'check_profile'),mock.patch.object(NativeProcessorRunner,'run',generate):
            result=process_pending(self.project,self.data,summary_reviewer=Reviewer())
        self.assertEqual('processed',result['status'],result)
        self.assertEqual(5,len(seen))
        self.assertEqual({generation_deadlines[0]},{r.deadline for r in seen})
        self.assertTrue(all(r.profile.to_dict()==result['execution_profile'] for r in seen))
        self.assertEqual(240,result['execution_profile']['timeout'])
        calls=json.loads(self.stored(result)[1][0]['execution_calls'])
        self.assertEqual({'generation','summary_support_1','summary_source_1','summary_outcome_1','summary_support_2','summary_outcome_2'},set(calls))
        self.assertEqual(7,2+len(seen))


class ArchivedInputStructuralPolicyTests(unittest.TestCase):
    from tests import test_summary_support as fixtures
    setUp=fixtures.SummarySupportTests.setUp
    stored=fixtures.SummarySupportTests.stored

    def test_authored_typed_replies_preserve_exact_marker_count_positives_and_block_false_language(self):
        import json
        from pathlib import Path
        from codex_mem.config import configure
        from codex_mem.processor import process_pending
        from codex_mem.store import Store
        from tests.test_summary_support import supported_review, source_output, receipt, outcome_output
        cases=json.loads((Path(__file__).parent/'fixtures/structural_summary_cases.json').read_text())
        for case in cases:
            with self.subTest(case=case['name']):
                self.data=self.data.parent/case['name']
                configure(self.data,capture_scope='all',service_enabled=False,semantic_enabled=False,jev_filter_enabled=False,jev_quality_enabled=False)
                for event in case['source_events']:
                    with Store(self.data) as store:
                        self.raw=store.remember(self.project,'Original synthetic source',event['text'],session_id='session',
                            source='hook:Stop' if event['role']=='assistant_report' else 'hook:UserPromptSubmit')
                candidate=copy.deepcopy(case['candidate']);seen=[]
                def review(request):
                    seen.append(request['version'])
                    if request['version']=='summary-source-v4':
                        return receipt(source_output(propositions=[dict(statement='The report includes the specified test marker.',
                            subject='Marker occurrence in this report',temporal_scope='This report only',modality='observed_text',source_id='report1',target='report_text')],
                            text_observations={'language':dict(status='observed',value='Polish prose with English technical terms.',scope='This report prose',reason=None),
                                'organization':dict(status='observed',value='Three bullet points with additional formatting details.',scope='This report list',reason=None)}))
                    result=supported_review(request)
                    if request['version']==SUPPORT_VERSION:
                        for row in support_rows(result['output']):
                            if row['unit_id'] in request['candidate_spans'] and row['unit_id']!='completed':
                                row['report_binding']=dict(target='other',properties=[])
                                row['citations']=[dict(ref,role='evidence') for ref in row['citations']]
                    elif request['version']=='summary-outcome-v6':
                        result['output']=outcome_output(request,'compatible')
                        if case['expected']=='contradicted':
                            pair=next(p for p in request['relations'] if p['source_item_id']=='observation_language')
                            next(row for row in result['output']['rows'] if row['relation_id']==pair['id'])['value']='incompatible'
                    return result
                def generate(request):
                    fixed=copy.deepcopy(candidate)
                    fixed['session_summary']['source_ids']=[source['id'] for source in request['sources']]
                    return receipt(fixed)
                result=process_pending(self.project,self.data,runner=generate,summary_reviewer=review)
                outputs=self.stored(result)[0]
                self.assertEqual([SUPPORT_VERSION,'summary-source-v4','summary-outcome-v6'],seen)
                if case['expected']=='contradicted':
                    self.assertEqual('summary_support_rejected',result.get('reason_code'),result)
                    self.assertEqual([],outputs)
                else:
                    self.assertEqual('processed',result['status'],result)
                    saved=outputs[0]['session_summary']
                    for field,value in candidate['session_summary'].items():
                        if field=='title':self.assertEqual(value,outputs[0]['title'])
                        elif field!='source_ids':self.assertEqual(value if value else None,saved[field])



class BindingRoutingTests(unittest.TestCase):
    from tests import test_summary_support as fixtures
    setUp = fixtures.SummarySupportTests.setUp
    stored = fixtures.SummarySupportTests.stored

    def saved_case(self):
        import json
        from pathlib import Path
        return json.loads((Path(__file__).parent/'fixtures/binding_routing_case.json').read_text())

    def prepared_case(self):
        from codex_mem import summary_outcome as outcome
        case=self.saved_case()
        claim=dict(sources=[dict(id=f'raw-{i}',body=event['text'],
            source='hook:Stop' if event['role']=='assistant_report' else 'hook:UserPromptSubmit')
            for i,event in enumerate(case['source_events'],1)],context=[])
        summary=case['candidate']['session_summary']
        _, _, payload=support.prepare(summary,claim)
        review=support.validate(authored_support_reply('binding-routing'),payload)
        _, _, source_payload=outcome.prepare_source(claim)
        source=outcome.validate_source(case['source'],source_payload)
        return case,summary,review,source,payload

    def test_saved_report_bound_claim_reaches_all_six_source_items(self):
        from codex_mem import summary_outcome as outcome
        _,summary,review,source,support_payload=self.prepared_case()
        _,schema,payload=outcome.prepare_outcome(summary,review,source,support_payload)
        self.assertEqual(6,len(payload['relations']))
        self.assertEqual({'p1','p2','p3','p4','observation_language','observation_organization'},
                         {row['source_item_id'] for row in payload['relations']})
        self.assertEqual({'completed#1'},{row['claim_id'] for row in payload['relations']})
        self.assertEqual({'exact'},{row['report_link'] for row in payload['relations']})
        self.assertEqual(('current2',),next(c.report_ids for c in review.claims if c.id=='completed#1'))
        self.assertEqual('reported_content',payload['claims']['completed#1']['target'])
        self.assertEqual(('content',),payload['claims']['completed#1']['properties'])
        self.assertEqual(summary['completed'],payload['units']['completed'])
        self.assertEqual(6,schema['properties']['rows']['minItems'])
        self.assertEqual(6,schema['properties']['rows']['maxItems'])


    def test_same_six_item_request_can_fail_unavailable_without_any_authored_semantic_verdict(self):
        from codex_mem.processor import process_pending, ProcessorFailure
        from codex_mem.store import Store
        from tests.test_summary_support import receipt
        case=self.saved_case();seen=[];observed=[]
        for event in case['source_events']:
            with Store(self.data) as store:
                self.raw=store.remember(self.project,'Saved source',event['text'],session_id='session',
                    source='hook:Stop' if event['role']=='assistant_report' else 'hook:UserPromptSubmit')
        def generate(request):
            candidate=copy.deepcopy(case['candidate'])
            candidate['session_summary']['source_ids']=[source['id'] for source in request['sources']]
            return receipt(candidate)
        def review(request):
            seen.append(request['version'])
            if request['version']==SUPPORT_VERSION:return receipt(authored_support_reply('binding-routing'))
            if request['version']=='summary-source-v4':return receipt(copy.deepcopy(case['source']))
            observed.append(request)
            # Authored operational failure only. No invented verdicts for the newly included pairs.
            raise ProcessorFailure('runner_unavailable')
        result=process_pending(self.project,self.data,runner=generate,summary_reviewer=review)
        self.assertEqual('summary_support_unavailable',result.get('reason_code'),result)
        self.assertEqual([],self.stored(result)[0])
        self.assertEqual([SUPPORT_VERSION,'summary-source-v4','summary-outcome-v6'],seen)
        self.assertEqual(6,len(observed[0]['relations']))
        self.assertEqual(case['candidate']['session_summary']['completed'],observed[0]['units']['completed'])
        self.assertEqual({'p1','p2','p3','p4','observation_language','observation_organization'},
                         {row['source_item_id'] for row in observed[0]['relations']})

    def test_six_items_allow_eight_claims_but_nine_fail_bounds_without_truncation(self):
        from codex_mem import summary_outcome as outcome
        _,summary,_,source,payload=self.prepared_case()
        # Manually authored capacity fixture, not additional model output.
        output=authored_support_reply('binding-routing')
        completed=output['units']['completed'][0]
        output['units']['completed']=[copy.deepcopy(completed) for _ in range(8)]
        review=support.validate(output,payload)
        _,schema,request=outcome.prepare_outcome(summary,review,source,payload)
        self.assertEqual(48,len(request['relations']))
        self.assertEqual(48,schema['properties']['rows']['maxItems'])
        title=output['units']['title'][0]
        title.update(report_binding=copy.deepcopy(completed['report_binding']),
            citations=copy.deepcopy(completed['citations']),candidate_assertion='Authored bound claim for capacity only.')
        review=support.validate(output,payload)
        with self.assertRaises(support.SupportError) as caught:outcome.prepare_outcome(summary,review,source,payload)
        self.assertEqual('bounds',caught.exception.subtype)
