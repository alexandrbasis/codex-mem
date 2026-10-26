"""Custody and synthetic protocol checks, not native semantic evidence."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from tests.support_contract_fixtures import authored_support_reply

from codex_mem import summary_outcome as outcome, summary_support as support
from tests import test_summary_support as fixtures


class OriginalEvidenceContractTests(unittest.TestCase):
    def build(self, body):
        summary=dict(title='Report',request='PRIVATE_COMPETING_REQUEST',completed='An attributed report with retained qualifiers.')
        claim=dict(sources=[dict(id='intent',source='hook:UserPromptSubmit',body='PRIVATE_COMPETING_REQUEST'),
            dict(id='original',source='hook:Stop',body=body,session_id='session',turn_id='turn',created_at='2026-10-25')],context=[])
        _, _, payload=support.prepare(summary,claim)
        output=fixtures.supported_review(payload)['output']
        review=support.validate(output,payload)
        prompt,schema,source_payload=outcome.prepare_source(claim)
        source=outcome.validate_source(fixtures.source_output(),source_payload)
        return summary,claim,payload,output,review,prompt,schema,source_payload,source

    def test_exact_unicode_multispan_body_survives_once_and_source_remains_candidate_blind(self):
        body='🙂 Początek. '+('Repeated value. '*180)+'\nFinal result not supplied.'
        summary,claim,payload,_,review,prompt,schema,source_payload,source=self.build(body)
        expected=hashlib.sha256(body.encode('utf-8')).hexdigest()
        self.assertEqual(expected,source['report'].get('body_sha256'))
        self.assertNotIn('body',source['report'])
        self.assertNotIn('PRIVATE_COMPETING_REQUEST',prompt)
        self.assertNotIn('body_sha256',json.dumps(schema))
        _,out_schema,request=outcome.prepare_outcome(summary,review,source,payload)
        self.assertEqual('summary-outcome-v5',request['version'])
        self.assertEqual(body,request['original_report']['body'])
        self.assertEqual(expected,request['original_report']['body_sha256'])
        self.assertEqual('current2',request['original_report']['catalog_id'])
        self.assertEqual(1,request['original_report']['current_index'])
        self.assertEqual('original',request['original_report']['provenance']['id'])
        self.assertGreater(len(payload['evidence'][1]['spans']),1)
        def bodies(value,path=''):
            if isinstance(value,dict):
                return [path+'.'+k for k in value if k=='body']+sum((bodies(v,path+'.'+k) for k,v in value.items()),[])
            if isinstance(value,list):return sum((bodies(v,path+'[]') for v in value),[])
            return []
        self.assertEqual(['.original_report.body'],bodies(request))

    def test_tampered_identity_and_complete_span_coverage_fail_closed(self):
        mutations={
            'hash':lambda p,s:s['report'].update(body_sha256='0'*64),
            'index':lambda p,s:s['report'].update(current_index=0),
            'catalog':lambda p,s:s['report'].update(catalog_id='history1'),
            'provenance-id':lambda p,s:s['report']['provenance'].update(id='foreign'),
            'provenance-turn':lambda p,s:s['report']['provenance'].update(turn_id='foreign'),
            'provenance-source':lambda p,s:s['report']['provenance'].update(source='hook:UserPromptSubmit'),
            'retained-id':lambda p,s:p['evidence'][1].update(retained_id='foreign'),
            'role':lambda p,s:p['evidence'][1].update(source='hook:UserPromptSubmit'),
            'derived':lambda p,s:p['evidence'][1].update(kind='session_summary'),
            'lifecycle':lambda p,s:p['evidence'][1].update(_jev_lifecycle_only=True),
            'group':lambda p,s:p['evidence'][1].update(evidence_group='history'),
            'foreign-span':lambda p,s:p['evidence'][1]['spans'][1].update(id='history1:2'),
            'duplicate-span':lambda p,s:p['evidence'][1]['spans'].insert(1,copy.deepcopy(p['evidence'][1]['spans'][0])),
            'overlap':lambda p,s:p['evidence'][1]['spans'][1].update(start=1199),
            'gap':lambda p,s:p['evidence'][1]['spans'][1].update(start=1201),
            'reordered':lambda p,s:p['evidence'][1]['spans'].reverse(),
            'incomplete':lambda p,s:p['evidence'][1]['spans'].pop(),
            'wrong-text':lambda p,s:p['evidence'][1]['spans'][1].update(text='Repeated value.'),
        }
        for label,mutate in mutations.items():
            with self.subTest(mutation=label):
                summary,_,payload,output,_,_,_,_,source=self.build('🙂 '+('Repeated value. '*190))
                mutate(payload,source)
                payload['report_catalog']=support.original_reports(payload['evidence'])
                with self.assertRaises(support.SupportError):
                    # Revalidate the supplied support premise so the report-specific guard is exercised.
                    review=support.validate(output,payload)
                    outcome.prepare_outcome(summary,review,source,payload)

    def test_hash_cannot_come_from_model_output_and_body_prompt_bounds_fail(self):
        from unittest import mock
        data=self.build('A complete source body.')
        summary,_,payload,output,review,_,_,source_payload,source=data
        altered=fixtures.source_output();altered['body_sha256']='0'*64
        with self.assertRaises(support.SupportError):outcome.validate_source(altered,source_payload)
        with self.assertRaises(support.SupportError) as caught:self.build('x'*100001)
        self.assertEqual('bounds',caught.exception.subtype)
        with mock.patch.object(outcome,'MAX_PROMPT_CHARS',128):
            with self.assertRaises(support.SupportError) as caught:outcome.prepare_outcome(summary,review,source,payload)
            self.assertEqual('bounds',caught.exception.subtype)
        # A fresh support premise does not authorize an overlong selected report at outcome.
        payload['evidence'][1]['body']='x'*100001
        payload['evidence'][1]['spans']=[dict(id=f'current2:{i//1200+1}',start=i,end=min(i+1200,100001),text=('x'*100001)[i:i+1200]) for i in range(0,100001,1200)]
        payload['report_catalog']=support.original_reports(payload['evidence'])
        review=support.validate(output,payload)
        with self.assertRaises(support.SupportError) as caught:outcome.prepare_outcome(summary,review,source,payload)
        self.assertEqual('bounds',caught.exception.subtype)

    def test_later_acknowledgement_cannot_supply_an_earlier_report_body(self):
        summary,claim,payload,output,review,_,_,_,source=self.build('Earlier complete report.')
        claim['sources'].append(dict(id='ack',source='hook:Stop',body='Acknowledged.'))
        _,_,later_payload=support.prepare(summary,claim)
        later_review=support.validate(output,later_payload)  # Anchors still refer to current2.
        with self.assertRaises(support.SupportError):outcome.prepare_outcome(summary,later_review,source,later_payload)
        _,_,source_payload=outcome.prepare_source(claim)
        current=outcome.validate_source(fixtures.source_output(),source_payload)
        _,_,request=outcome.prepare_outcome(summary,later_review,current,later_payload)
        self.assertEqual([],request['relations'])
        self.assertEqual('Acknowledged.',request['original_report']['body'])
        self.assertFalse(outcome.outcome_applicable(request))

    def test_known_saved_support_source_requests_keep_42_and_30_relations(self):
        cases=json.loads((Path(__file__).parent/'fixtures/outcome_original_evidence_cases.json').read_text())
        for case in cases:
            with self.subTest(case=case['id']):
                claim=dict(sources=[dict(id=f'raw-{i}',body=e['text'],source='hook:Stop' if e['role']=='assistant_report' else 'hook:UserPromptSubmit') for i,e in enumerate(case['source_events'],1)],context=[])
                summary=case['candidate']['session_summary']
                _,_,payload=support.prepare(summary,claim)
                review=support.validate(authored_support_reply(case['id']),payload)
                _,_,source_payload=outcome.prepare_source(claim)
                source=outcome.validate_source(case['source'],source_payload)
                _,schema,request=outcome.prepare_outcome(summary,review,source,payload)
                self.assertEqual(case['expected_relations'],len(request['relations']))
                self.assertEqual(case['expected_relations'],schema['properties']['rows']['maxItems'])
                self.assertEqual(source_payload['report']['body'],request['original_report']['body'])
                self.assertEqual(summary['completed'],request['units']['completed'])

    def test_v5_cache_identity_and_legacy_diagnostics_do_not_relabel_old_replies(self):
        from unittest import mock
        summary,claim,payload,_,review,_,_,_,source=self.build('Original report.')
        key=support.rejection_key(summary,'evidence',source,{'model':'fixed'})
        with mock.patch.object(outcome,'OUTCOME_VERSION','summary-outcome-v4'):
            self.assertNotEqual(key,support.rejection_key(summary,'evidence',source,{'model':'fixed'}))
        legacy=support.diagnostic(summary,claim,status='uncertain',version='summary-outcome-v4')
        self.assertEqual(legacy,support.safe_diagnostic(legacy))
        _,_,request=outcome.prepare_outcome(summary,review,source,payload)
        old=dict(version='summary-outcome-v4',rows=[])
        with self.assertRaises(support.SupportError):outcome.validate_outcome(old,request)


class OriginalEvidencePublicTests(unittest.TestCase):
    setUp=fixtures.SummarySupportTests.setUp
    stored=fixtures.SummarySupportTests.stored

    def test_public_request_gets_exact_source_and_failed_reply_or_changed_custody_writes_nothing(self):
        from codex_mem.config import configure
        from codex_mem.processor import process_pending
        from codex_mem.store import Store
        body='Report with an unresolved finding. 🙂 '+('Repeated observation. '*65)+'\nNo external proof supplied.'
        for scenario in ('unused','uncertain','bad-reply','changed-custody','prompt-overflow'):
            with self.subTest(scenario=scenario):
                from unittest import mock
                self.data=self.data.parent/scenario
                configure(self.data,capture_scope='all',service_enabled=False,semantic_enabled=False,jev_filter_enabled=False,jev_quality_enabled=False)
                with Store(self.data) as store:self.raw=store.remember(self.project,'Report',body,source='hook:Stop',session_id='session')
                seen=[];captured=[]
                def generate(request):
                    return fixtures.receipt(dict(disposition='processed',notes=[],session_summary=dict(title='Report',request=None,investigated='',learned='',completed='The report records an unresolved finding.',next_steps='',notes='',source_ids=[request['sources'][0]['id']])))
                def review(request):
                    seen.append(request['version'])
                    result=fixtures.supported_review(request)
                    if request['version']=='summary-source-v4':
                        if scenario=='changed-custody':
                            with Store(self.data) as store:store._connection.execute('UPDATE entries SET body=? WHERE id=?',('Changed after extraction',self.raw['id']))
                        if scenario=='prompt-overflow':patch.start()
                    if request['version']=='summary-outcome-v5':
                        captured.append(request)
                        self.assertEqual(body,request['original_report']['body'])
                        self.assertNotEqual(body,request['units']['completed'])
                        if scenario=='uncertain':result['output']=fixtures.outcome_output(request,'unresolved')
                        if scenario=='bad-reply':result['output']['version']='summary-outcome-v4'
                    return result
                patch=mock.patch.object(outcome,'MAX_PROMPT_CHARS',128)
                try:result=process_pending(self.project,self.data,runner=generate,summary_reviewer=review)
                finally:patch.stop()
                records=self.stored(result)[0]
                if scenario=='unused':
                    self.assertEqual('processed',result['status'],result)
                    self.assertEqual(1,len(records))
                else:
                    self.assertEqual([],records)
                    expected={'uncertain':'summary_support_uncertain','bad-reply':'summary_support_invalid',
                              'changed-custody':'summary_support_stale','prompt-overflow':'summary_support_invalid'}[scenario]
                    self.assertEqual(expected,result.get('reason_code'),result)
                self.assertEqual(0 if scenario in ('changed-custody','prompt-overflow') else 1,len(captured))
                if scenario=='prompt-overflow':self.assertEqual('bounds',result['summary_support'][-1]['invalid_subtype'])
