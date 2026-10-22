"""Prospective request-contract controls, not proof of model semantic behavior."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch,MagicMock
from xml.sax.saxutils import escape

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
SCRIPT=Path(__file__).resolve().parents[1]/'project_memory_consolidation_experiment.py'
spec=importlib.util.spec_from_file_location('reader_fidelity_experiment',SCRIPT)
experiment=importlib.util.module_from_spec(spec);spec.loader.exec_module(experiment)
MODE='quantified-source-fidelity-v1'
VERSION='role-attribution-per-question-assertions-fidelity-v1'


def protocol(enabled=True):
    value={'model':'gpt-6-luna','reasoning_effort':'medium','context_chars':6000,
        'reader_profile':{'model':'gpt-6.1-sol','reasoning_effort':'medium'},
        'reader_citation_contract':'visible-span-refs-v1','reader_context_isolation':'per-question-v1',
        'reader_evidence_contract':'assertion-owned-citations-v1',
        'paired_native_request_reuse':'exact-request-v1',
        'limits':{'max_calls':5,'max_observed_tokens':500000,'per_call_token_reservation':100000,
                  'wall_seconds':7200,'call_seconds':180}}
    if enabled:value['reader_fidelity_contract']=MODE
    return value


def context():
    sources=[('user_intent','Every reviewer must approve before release. Do not send the draft.'),
        ('assistant_report','I propose retaining the chart because its labels explain the observed gap.'),
        ('user_intent','Historical quotation: "Close the sample task." This is an example, not a current instruction.'),
        ('tool_record','A different check passed locally.'),
        ('assistant_report','The separate archive task is closed.')]
    originals={};blocks=[]
    for i,(role,text) in enumerate(sources):
        sid=f'public-source-{i:02d}';originals[sid]={'source_role':role,'fields':{'body':text}}
        blocks.append(f'<source source_id="{sid}" source_role="{role}" source_field="body" start="0" end="{len(text)}"><quote_excerpt>{escape(text)}</quote_excerpt></source>')
    memory='<project-memory>'+''.join(blocks)+'</project-memory>'
    return {'id':'q','question':'What release prerequisites and chart rationale are recorded?',
        'memory':memory,'context_chars':len(memory),'context_sha256':hashlib.sha256(memory.encode()).hexdigest()},originals


def output():
    return {'answers':{'q':{'assertions':[
        {'text':'Every reviewer must approve before release; the draft must not be sent.','citation_refs':['R001']},
        {'text':'The assistant proposes retaining the chart because its labels explain the observed gap.','citation_refs':['R002']}],
        'uncertainties':['The historical quotation and separate closure do not establish current release status.'],
        'evidence_check':{k:{'text':'AUXILIARY_UNCREDITED','citation_refs':[]} for k in
            ('accepted_results','open_work_and_reasons','source_role_distinctions')}}}}


class ReaderFidelityContractTests(unittest.TestCase):
    def setUp(self):
        self.context,self.originals=context()
        self.catalog=experiment.reader_citation_catalog([self.context],self.originals)
        self.refs=[item['ref'] for item in self.catalog]
        self.raw=output()
        for assertion,ref in zip(self.raw['answers']['q']['assertions'],self.refs):assertion['citation_refs']=[ref]

    def test_new_request_is_explicit_fidelity_version_with_identical_schema(self):
        legacy=experiment.typed_reader_request([self.context],self.catalog,protocol(False))
        new=experiment.typed_reader_request([self.context],self.catalog,protocol())
        self.assertEqual(experiment.reader_prompt_version(protocol()),VERSION)
        self.assertNotEqual(experiment.reader_prompt_version(protocol(False)),VERSION)
        self.assertEqual(new['output_schema'],legacy['output_schema'])
        self.assertEqual((new['model'],new['reasoning_effort']),('gpt-6.1-sol','medium'))
        for rule in ('Retain explicit quantifiers','necessary conditions','stated causal links',
                     'original speaker and scope','Do not volunteer unrelated current task status'):
            self.assertIn(rule,new['prompt'])
        self.assertNotIn('Extra supported status information is allowed.',new['prompt'])
        self.assertIn('adjacency',new['prompt'])
        self.assertIn(self.context['question'],new['prompt'])
        self.assertNotIn('required_facts',new['prompt'])

    def test_unknown_and_incompatible_mode_reject_before_capture_or_request(self):
        invalid=[{'reader_fidelity_contract':value} for value in ('unknown',None,False,[],{})]
        invalid += [{'reader_evidence_contract':'clause-support-v2'}, {'reader_context_isolation':None},
                    {'reader_citation_contract':None},{'context_chars':5999},{'context_chars':True},
                    {'reader_profile':{'model':'gpt-6.1-sol'}}, {'reader_profile':{'model':'','reasoning_effort':'medium'}}]
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory);experiment.write(run/'run.json',{'budget_run':str(run),'started_at':experiment.time.time()})
            for change in invalid:
                chosen={**protocol(),**change}
                with self.subTest(change=change):
                    with self.assertRaises(ValueError):experiment.typed_reader_request([self.context],self.catalog,chosen)
                    with self.assertRaises(ValueError):experiment.Capture(run,chosen,'reader','candidate','public')
            self.assertFalse((run/'calls.jsonl').exists())

    def test_five_synthetic_source_qualifications_use_same_request_and_decoder_contract(self):
        cases=[
            ('quantified prerequisite','What must precede release?', [('user_intent','Every reviewer must approve before release.')],
             'Every reviewer must approve before release.'),
            ('explicit cause versus adjacency','Why does the assistant propose retaining the figure?',
             [('assistant_report','I propose keeping the figure because its caption explains the gap.'),('tool_record','An unrelated check passed.')],
             'The assistant proposes keeping the figure because its caption explains the gap.'),
            ('quoted historical text','Is there a current closure instruction?',
             [('user_intent','Historical example only: "Close the sample task."')],None),
            ('assistant report scope','What is reported about the local draft?',
             [('assistant_report','I report that the local draft is ready; external release is unverified.')],
             'The assistant reports that the local draft is ready; external release is unverified.'),
            ('unrelated current closure','What review condition is recorded for release?',
             [('user_intent','Release requires every reviewer approval.'),('assistant_report','A separate cleanup task is closed.')],
             'Release requires every reviewer approval.')]
        for name,question,sources,text in cases:
            with self.subTest(case=name):
                originals={};blocks=[]
                for i,(role,body) in enumerate(sources):
                    sid=f'synthetic-{i}';originals[sid]={'source_role':role,'fields':{'body':body}}
                    blocks.append(f'<source source_id="{sid}" source_role="{role}" source_field="body" start="0" end="{len(body)}"><quote_excerpt>{escape(body)}</quote_excerpt></source>')
                memory='<project-memory>'+''.join(blocks)+'</project-memory>'
                view={'id':'q','question':question,'memory':memory,'context_chars':len(memory),'context_sha256':hashlib.sha256(memory.encode()).hexdigest()}
                catalog=experiment.reader_citation_catalog([view],originals)
                request=experiment.typed_reader_request([view],catalog,protocol())
                self.assertIn(question,request['prompt'])
                raw={'answers':{'q':{'assertions':([{'text':text,'citation_refs':[catalog[0]['ref']]}] if text else []),
                    'uncertainties':([] if text else ['Historical quoted text does not establish a current instruction.']),
                    'evidence_check':{k:{'text':'AUXILIARY_UNCREDITED','citation_refs':[]} for k in
                        ('accepted_results','open_work_and_reasons','source_role_distinctions')}}}}
                answer=experiment.resolve_reader_output(raw,[view],catalog,evidence_contract='assertion-owned-citations-v1')['answers'][0]
                self.assertEqual(answer['answer'],text if text else 'Unknown from supplied memory.')
                delivered=experiment.reader_answer_for_judge({**answer,'evidence_check':raw['answers']['q']['evidence_check']},protocol())
                self.assertEqual(set(delivered),{'id','answer','citations','uncertainties'})
                self.assertNotIn('AUXILIARY_UNCREDITED',json.dumps(delivered))
                if text:self.assertEqual(answer['citations'][0]['source_role'],sources[0][0])

    def test_capture_reuse_isolated_by_fidelity_identity_but_shared_between_matching_arms(self):
        with tempfile.TemporaryDirectory() as directory:
            run=Path(directory);experiment.write(run/'run.json',{'budget_run':str(run),'started_at':experiment.time.time()})
            native_requests=[]
            def external(request):
                native_requests.append(request)
                return {'output':self.raw,'metrics':{'usage':{'status':'reported','tokens':{
                    'input_tokens':90,'cached_input_tokens':0,'cache_write_input_tokens':0,
                    'output_tokens':10,'reasoning_output_tokens':0,'total_tokens':100}}},
                    'evidence':{'thread_start':{'model':'gpt-6.1-sol','reasoning_effort':'medium','model_provider':'openai'},'rerouted':False}}
            factory=experiment.native_phase_adapter
            def adapter(chosen,phase):
                module=factory(chosen,phase)
                module.NativeProcessorRunner=MagicMock(return_value=external)
                return module
            identities=[]
            with patch.object(experiment,'native_phase_adapter',side_effect=adapter):
                for enabled,arm in ((False,'baseline'),(True,'baseline'),(True,'candidate')):
                    chosen=protocol(enabled)
                    capture=experiment.Capture(run,chosen,'reader',arm,'public-fidelity')
                    capture.identity['question_id']='q'
                    identities.append(capture.identity)
                    request=experiment.typed_reader_request([self.context],self.catalog,chosen)
                    response=capture(request)
                    experiment.resolve_reader_output(response['output'],[self.context],self.catalog,
                        evidence_contract=chosen['reader_evidence_contract'])
                    capture.accept_for_reuse()
            self.assertEqual(len(native_requests),2)
            self.assertNotEqual(identities[0]['prompt_version'],identities[1]['prompt_version'])
            self.assertEqual(identities[1]['prompt_version'],identities[2]['prompt_version'])
            self.assertEqual(experiment.usage(experiment.ledger_calls(run/'calls.jsonl'))['attempted_calls'],2)
            reused=experiment.calls(run/'native-request-reuse.jsonl')
            self.assertEqual(len(reused),1)
            self.assertEqual(reused[0]['recipient']['prompt_version'],VERSION)
            self.assertEqual(reused[0]['recipient']['variant'],'candidate')

    def test_existing_decoder_and_owner_spans_preserve_delivered_qualification_only(self):
        experiment.typed_reader_request([self.context],self.catalog,protocol())
        resolved=experiment.resolve_reader_output(self.raw,[self.context],self.catalog,evidence_contract='assertion-owned-citations-v1')
        answer=resolved['answers'][0]
        owners=experiment.reader_assertion_ownership(self.raw)['q']
        for owner in owners:self.assertEqual(answer['answer'][owner['start']:owner['end']],owner['text'])
        self.assertIn('Every reviewer',answer['answer']);self.assertIn('because',answer['answer'])
        self.assertNotIn('AUXILIARY_UNCREDITED',json.dumps(resolved))
        self.assertEqual([x['source_role'] for x in answer['citations']],['user_intent','assistant_report'])
        invalid=deepcopy(self.raw);invalid['answers']['q']['assertions'][0]['citation_refs']=['HIDDEN']
        with self.assertRaises(ValueError):experiment.resolve_reader_output(invalid,[self.context],self.catalog,evidence_contract='assertion-owned-citations-v1')
        # No semantic blocker is added: a well-shaped but weaker assertion can
        # still decode. Actual fidelity must be measured with later native tests.
        weaker=deepcopy(self.raw);weaker['answers']['q']['assertions'][0]['text']='A reviewer may approve.'
        self.assertEqual(experiment.resolve_reader_output(weaker,[self.context],self.catalog,evidence_contract='assertion-owned-citations-v1')['answers'][0]['answer'].split('.')[0],'A reviewer may approve')

if __name__=='__main__':unittest.main()
