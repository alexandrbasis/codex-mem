"""Public invented structural contract tests. No native calls or semantic grades."""
import copy
import hashlib
from xml.sax.saxutils import escape
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import json
import tempfile
import types
import sys

ROOT=next((parent for parent in Path(__file__).resolve().parents
           if (parent/'scripts/project_memory_consolidation_experiment.py').is_file()),Path.cwd())
sys.path.insert(0,str(ROOT))
HELPER=Path(os.environ.get('READER_HELPER',ROOT/'scripts/project_memory_consolidation_experiment.py'))
spec=importlib.util.spec_from_file_location('reader_assertion_helper',HELPER)
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
NEW='assertion-owned-citations-v1'
PROTOCOL={'model':'gpt-6.1-sol','reasoning_effort':'medium','reader_context_isolation':'per-question-v1','reader_citation_contract':'visible-span-refs-v1','reader_evidence_contract':NEW,'context_chars':6000}
CAT=[{'ref':'R0001','source_id':'public-user','source_field':'body','source_role':'user_intent','start':0,'end':26,'quote':'Keep amber markers intact.'}, {'ref':'R0002','source_id':'public-report','source_field':'body','source_role':'assistant_report','start':0,'end':24,'quote':'I uploaded the draft map.'}]
CONTEXT=[{'id':'P1','question':'What remains required?','memory':'<project-memory/>','context_chars':17}]
def packet():
    return {'answers':{'P1':{'assertions':[{'text':'Keep amber markers intact.','citation_refs':['R0001']}], 'uncertainties':[], 'evidence_check':{k:{'text':'The assistant reported an upload.','citation_refs':['R0002']} for k in ('accepted_results','open_work_and_reasons','source_role_distinctions')}}}}
class OwnershipTests(unittest.TestCase):
    def resolve(self,p):return h.resolve_reader_output(p,CONTEXT,CAT,evidence_contract=NEW)
    def test_checklist_refs_never_flatten_into_delivered_citations(self):
        p=packet();out=self.resolve(p)['answers'][0]
        self.assertEqual(out['answer'],'Keep amber markers intact.')
        self.assertEqual([v['catalog_ref'] for v in out['citations']],['R0001'])
        self.assertEqual(set(out),{'id','answer','citations','uncertainties'})
    def test_extra_delivered_status_allowed_and_owned(self):
        p=packet();p['answers']['P1']['assertions'].append({'text':'The assistant reported uploading the map.','citation_refs':['R0002']})
        out=self.resolve(p)['answers'][0]
        self.assertEqual(len(out['citations']),2)
        ownership=h.reader_assertion_ownership(p)['P1']
        self.assertEqual(ownership[1]['citation_refs'],['R0002'])
        self.assertEqual(out['answer'][ownership[1]['start']:ownership[1]['end']],ownership[1]['text'])
    def test_preserve_whitespace_bytes_and_exact_owned_slices(self):
        p=packet();p['answers']['P1']['assertions']=[
            {'text':'  Keep amber markers intact.\t','citation_refs':['R0001']},
            {'text':'\nThe assistant reported uploading.  ','citation_refs':['R0002']}]
        p['answers']['P1']['uncertainties']=['  The route is not evidenced.\n']
        p['answers']['P1']['evidence_check']['accepted_results']['text']='  No acceptance evidenced.\n'
        out=self.resolve(p)['answers'][0]
        self.assertEqual(out['answer'],'  Keep amber markers intact.\t \nThe assistant reported uploading.  ')
        self.assertEqual(out['uncertainties'],['  The route is not evidenced.\n'])
        owners=h.reader_assertion_ownership(p)['P1']
        for index,owner in enumerate(owners):
            self.assertEqual(out['answer'][owner['start']:owner['end']],p['answers']['P1']['assertions'][index]['text'])
        self.assertEqual(owners[1]['start'],len(owners[0]['text'])+1)
    def test_reject_whitespace_only_owner_auxiliary_and_uncertainty(self):
        for target in ('owner','auxiliary','uncertainty'):
            with self.subTest(target=target):
                p=packet();a=p['answers']['P1']
                if target=='owner':a['assertions'][0]['text']=' \t\n '
                elif target=='auxiliary':a['evidence_check']['accepted_results']['text']=' \t\n '
                else:a['uncertainties']=[' \t\n ']
                with self.assertRaises(ValueError):self.resolve(p)
    def test_order_and_dedup_keep_each_owner(self):
        p=packet();p['answers']['P1']['assertions'].append({'text':'The map keeps its markers.','citation_refs':['R0001']})
        self.assertEqual(len(self.resolve(p)['answers'][0]['citations']),1)
        self.assertEqual(len(h.reader_assertion_ownership(p)['P1']),2)
    def test_unknown_branch_has_no_invented_citations(self):
        p=packet();a=p['answers']['P1'];a['assertions']=[];a['uncertainties']=['The next visit is not evidenced.']
        out=self.resolve(p)['answers'][0];self.assertEqual(out['answer'],'Unknown from supplied memory.');self.assertEqual(out['citations'],[])
    def test_reject_invalid_whole_packets(self):
        mutations=[lambda a:a['assertions'][0].update(citation_refs=[]),lambda a:a['assertions'][0].update(citation_refs=['R9999']),lambda a:a['assertions'][0].update(text=''),lambda a:a['assertions'][0].update(text=' \t\n '),lambda a:a.update(citation_refs=['R0002']),lambda a:a.update(answer='detached text'),lambda a:a.update(assertions=[]),lambda a:a['evidence_check']['accepted_results'].update(citation_refs=['R9999']),lambda a:a['assertions'][0].update(citation_refs=['R0001','R0001']),lambda a:a.update(assertions=a['assertions']*13),lambda a:a['assertions'][0].update(text='x'*2001),lambda a:a.update(uncertainties=['']),lambda a:a['assertions'][0].update(extra=True)]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                p=packet();mutate(p['answers']['P1'])
                with self.assertRaises(ValueError):self.resolve(p)
        for missing in ({'answers':{}},{'answers':{'P2':packet()['answers']['P1']}},{'answers':packet()['answers'],'extra':True}):
            with self.assertRaises(ValueError):self.resolve(missing)
    def test_structural_ownership_does_not_establish_entailment(self):
        p=packet();p['answers']['P1']['assertions'][0]['text']='The moon is made of linen.'
        self.assertIn('linen',self.resolve(p)['answers'][0]['answer'])
    def test_new_prompt_schema_is_opt_in(self):
        self.assertEqual(h.reader_prompt_version(PROTOCOL),'role-attribution-per-question-assertions-v1')
        req=h.typed_reader_request(CONTEXT,CAT,PROTOCOL)
        schema=req['output_schema']['properties']['answers']['properties']['P1']['properties']
        self.assertIn('assertions',schema);self.assertNotIn('answer',schema);self.assertNotIn('citation_refs',schema)
        self.assertIn('assertion',req['prompt']);self.assertNotIn('public-report',req['prompt'])
        with self.assertRaises(ValueError):h.reader_prompt_version({**PROTOCOL,'reader_context_isolation':None})
    def test_v2_behavior_unchanged(self):
        p={'answers':{'P1':{'answer':'Keep the markers.','citation_refs':['R0001','R0002'],'uncertainties':[],'evidence_check':{k:'not evidenced' for k in ('accepted_results','open_work_and_reasons','source_role_distinctions')}}}}
        v2={**PROTOCOL,'reader_evidence_contract':'clause-support-v2'}
        self.assertEqual(h.reader_prompt_version(v2),'role-attribution-per-question-v2')
        self.assertEqual(len(h.resolve_reader_output(p,CONTEXT,CAT)['answers'][0]['citations']),2)
        self.assertEqual(h.reader_answer_for_judge({**self.resolve(packet())['answers'][0],'assertion_ownership':['private']},PROTOCOL),self.resolve(packet())['answers'][0])
    def test_old_requests_and_resolver_match_unmodified_helper(self):
        base_path=ROOT/'docs/evidence/project-memory-quality-2026-10-21-r18/frozen-helper.py'
        self.assertEqual(hashlib.sha256(base_path.read_bytes()).hexdigest(),
                         'd935fe07687571bf42271e4a9606c9a98a75a162beb8a050f4b7fcc95960a388')
        spec=importlib.util.spec_from_file_location('reader_unchanged_base',base_path)
        base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
        public_catalog=[{**entry,'end':len(entry['quote'])} for entry in CAT]
        memory='<project-memory>'+''.join(
            '<source source_id="'+entry['source_id']+'" source_field="'+entry['source_field']+
            '" source_role="'+entry['source_role']+'" start="0" end="'+str(entry['end'])+
            '"><quote_excerpt>'+escape(entry['quote'])+'</quote_excerpt></source>'
            for entry in public_catalog)+'</project-memory>'
        public_context=[{**CONTEXT[0],'memory':memory,'context_chars':len(memory)}]
        originals={entry['source_id']:{'source_role':entry['source_role'],
                   'fields':{entry['source_field']:entry['quote']}} for entry in public_catalog}
        self.assertEqual([{k:v for k,v in entry.items() if k!='ref'}
                          for entry in h.reader_citation_catalog(public_context,originals)],
                         [{k:v for k,v in entry.items() if k!='ref'} for entry in public_catalog])
        old_packet={'answers':{'P1':{'answer':'Keep amber markers intact.',
            'citation_refs':['R0001','R0002'],'uncertainties':[],
            'evidence_check':{k:'not evidenced' for k in ('accepted_results','open_work_and_reasons','source_role_distinctions')}}}}
        self.assertEqual(h.resolve_reader_output(old_packet,public_context,public_catalog),
                         base.resolve_reader_output(old_packet,public_context,public_catalog))
        for evidence in (None,'clause-support-v2'):
            protocol={**PROTOCOL};protocol.pop('reader_evidence_contract')
            if evidence:protocol['reader_evidence_contract']=evidence
            for contexts,catalog in ((public_context,public_catalog),(CONTEXT,[])):
                actual=h.typed_reader_request(contexts,catalog,protocol)
                original=base.typed_reader_request(contexts,catalog,protocol)
                self.assertEqual(json.dumps(actual,ensure_ascii=False,separators=(',',':')).encode(),
                                 json.dumps(original,ensure_ascii=False,separators=(',',':')).encode())
    def test_whole_text_bound_and_context_budget_reject_without_clipping(self):
        p=packet();p['answers']['P1']['assertions']=[{'text':'a'*1600,'citation_refs':['R0001']} for _ in range(4)]
        with self.assertRaises(ValueError):self.resolve(p)
        with self.assertRaises(ValueError):h.typed_reader_request([*CONTEXT,*CONTEXT],CAT,PROTOCOL)
        long=[{**CONTEXT[0],'memory':'x'*6001}]
        with self.assertRaises(ValueError):h.typed_reader_request(long,[],PROTOCOL)
    def test_durable_ownership_roundtrip_and_tamper_rejection(self):
        context=[{**CONTEXT[0],'memory':'<project-memory><source source_id="public-user" source_field="body" source_role="user_intent" start="0" end="26"><quote_excerpt>Keep amber markers intact.</quote_excerpt></source></project-memory>'}]
        catalog=CAT[:1]
        originals={'public-user':{'source_role':'user_intent','fields':{'body':'Keep amber markers intact.'}}}
        raw=packet()
        for check in raw['answers']['P1']['evidence_check'].values():check['citation_refs']=[]
        metrics={'duration_ms':1,'usage':{'status':'reported','tokens':dict(input_tokens=10,cached_input_tokens=0,cache_write_input_tokens=0,output_tokens=2,reasoning_output_tokens=0,total_tokens=12)}}
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);destination=home/'baseline'/'public-fiction';destination.mkdir(parents=True)
            h.write(home/'run.json',{'budget_run':str(home)})
            class FakeCapture:
                def __init__(self,*args):self.identity={};self.native_call_id='call-0001'
                def __call__(self,request):
                    response={'output':raw,'metrics':metrics}
                    h.write(home/'receipts/call-0001-request.json',request);h.write(home/'receipts/call-0001-response.json',response)
                    return response
            args=types.SimpleNamespace(run=home,variant='baseline')
            with patch.object(h,'Capture',FakeCapture):
                h.isolated_readers(args,PROTOCOL,{'id':'public-fiction'},destination,context,originals,catalog,0,'state-pin','complete')
            reader=destination/'reader-0.json';saved=h.read(reader)
            h.validate_isolated_reader(destination,reader,saved,{'P1':context[0]},{'P1':catalog},home,PROTOCOL)
            self.assertEqual(saved['assertion_ownership']['P1'][0]['text'],'Keep amber markers intact.')
            self.assertNotIn('evidence_check',saved['output']['answers'][0])
            part_path=destination/'reader-0.q-P1.json';part=h.read(part_path)
            part['assertion_ownership']['P1'][0]['start']=3;h.write(part_path,part)
            saved['question_receipts'][0]['receipt_sha256']=h.digest(part)
            with self.assertRaises(ValueError):h.validate_isolated_reader(destination,reader,saved,{'P1':context[0]},{'P1':catalog},home,PROTOCOL)
if __name__=='__main__':unittest.main()

