"""Public disclosed controls only; external native boundary is always stubbed."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import unittest
import hashlib
import tempfile
from unittest.mock import patch, MagicMock

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
helper=load('r23_actual_helper',ROOT/'scripts/project_memory_consolidation_experiment.py')
probe=load('r23_probe',HERE/'prepare-reader-probe.py')


def native_readers(prepared, assertion=True):
    outputs=[]
    for ref in prepared['evaluator_references_not_reader_input']:
        qid=ref['context']['id']; refs=list(dict.fromkeys(span['ref'] for span in ref['catalog']))[:12]
        text=' '.join(fact['text'] for fact in ref['gold']['required_facts'])
        checks={k:({'text':'AUXILIARY_ONLY_SENTINEL','citation_refs':[]} if assertion else 'AUXILIARY_ONLY_SENTINEL')
            for k in ('accepted_results','open_work_and_reasons','source_role_distinctions')}
        answer=({'assertions':[{'text':text,'citation_refs':refs}],'uncertainties':[],'evidence_check':checks}
            if assertion else {'answer':text,'citation_refs':refs,'uncertainties':[],'evidence_check':checks})
        outputs.append({'answers':{qid:answer}})
    return outputs


def native_grades(payload, expected=None):
    """Explicit synthetic native packet, never evidence of native accuracy."""
    grades={}
    for case in payload['cases']:
        facts=expected[case['id']]['fact_recall'] if expected else [True]*len(case['gold']['required_facts'])
        errors=expected[case['id']]['critical_errors'] if expected else []
        sources=payload['case_source_catalogs'][case['id']]
        source=next((s for s in sources if s['reader_visible']),sources[0])
        grades[case['id']]={'full_supported_answer':all(facts) and not errors,
            'current_state_actionable':True,'constraints_obligations_reasons_recalled':True,
            'citations_supported':helper.judge_citation_bounds(case,sources),'empty_or_evasive':False,
            'critical_errors':errors,'rationale':'Synthetic decoder exercise, no model accuracy claim.',
            'fact_recall':{str(i):{'recalled':ok,'answer_refs':['A0001'] if ok else [],
                'source_refs':[source['ref']] if ok else [],'omission_reason':'not_omitted' if ok else 'semantic_omission',
                'missing_anchor':''} for i,ok in enumerate(facts)},
            'critical_witnesses':[{'error_type':e,'answer_ref':'A0001','assertion_mode':'affirmative_current',
                'relation':'asserts_forbidden_conclusion','prohibition_ref':'policy:'+e} for e in errors]}
    return {'grades':grades}


class ReaderContractCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.corpus=json.loads((HERE/'public-reader-regressions.json').read_text())
        self.protocol=probe.default_protocol(self.corpus)
        self.protocol['reader_evidence_contract']='assertion-owned-citations-v1'

    def test_six_public_assertion_packets_resolve_and_judge_with_actual_helper(self):
        prepared=probe.prepare(helper,self.protocol,self.corpus)
        self.assertEqual(len(prepared['reader_requests']),8)
        for item in prepared['reader_requests']:
            schema=item['request']['output_schema']['properties']['answers']['properties']
            self.assertIn('assertions',next(iter(schema.values()))['properties'])
            self.assertEqual(item['request']['model'],'gpt-6.1-sol')
            self.assertNotIn('required_facts',item['request']['prompt'])
        outputs=native_readers(prepared)
        payload=probe.judge_payload(helper,self.protocol,self.corpus,outputs)
        self.assertNotIn('AUXILIARY_ONLY_SENTINEL',json.dumps(payload))
        self.assertEqual(len(payload['cases']),8)
        request=probe.judge_request(helper,self.protocol,self.corpus,outputs)
        self.assertEqual(request['model'],'gpt-6-astra')
        grades=helper.decode_judge_output(native_grades(payload),payload['cases'],self.protocol,payload['case_source_catalogs'])
        self.assertTrue(all(g['full_supported_answer'] and not g['critical_errors'] for g in grades))
        malformed=deepcopy(outputs);first=next(iter(malformed[0]['answers'].values()))
        first['assertions'][0]['citation_refs']=['NOT_DISPLAYED']
        with self.assertRaises(ValueError):probe.judge_payload(helper,self.protocol,self.corpus,malformed)
        with self.assertRaises(ValueError):probe.judge_payload(helper,self.protocol,self.corpus,outputs[:-1])

    def test_public_runner_all_eight_calls_use_actual_capture_and_decoders_offline(self):
        self.public_runner_case()

    def test_public_runner_preserves_rejected_raw_packet_without_retry_or_grade(self):
        self.public_runner_case(invalid=True)

    def public_runner_case(self,invalid=False):
        runner=load('r23_runner_e2e',HERE/'run-public-preflight.py')
        library=load('r23_calibration_e2e',HERE/'public-calibration-library.py')
        protocol=json.loads((ROOT/'docs/evidence/project-memory-quality-2026-10-21-r16/protocol.json').read_text())
        protocol.update(self.protocol)
        protocol['public_preflight'].update(attempt_id='synthetic-r23-offline',fresh_reader_calls=8,fresh_public_reader_judge_packets=1,fresh_known_control_judge_packets=1,planned_native_calls=10,reader_fidelity_contract='quantified-source-fidelity-v1')
        protocol['judge_calibration']['attempt_id']='synthetic-r23-offline'
        paths=list((ROOT/'codex_mem').glob('*.py'))+[ROOT/'scripts/project_memory_consolidation_experiment.py']
        paths += [HERE/name for name in ('run-public-preflight.py','prepare-reader-probe.py','public-reader-regressions.json','public-calibration-library.py','judge-contract-controls.json')]
        protocol['public_preflight']['file_sha256']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        prepared=probe.prepare(helper,protocol,self.corpus)
        outputs=native_readers(prepared)
        if invalid:
            next(iter(outputs[0]['answers'].values()))['assertions'][0]['citation_refs']=['NOT_DISPLAYED']
        descriptor,payload=library.prepare_packets(helper,json.loads((HERE/'judge-contract-controls.json').read_text()))[-1]
        responses=[native_grades(payload,descriptor['expected']),*outputs]
        if not invalid:responses.append(native_grades(probe.judge_payload(helper,protocol,self.corpus,outputs)))
        observed=[]
        def external(request):
            output=responses[len(observed)];observed.append(request)
            return {'output':output,'metrics':{'usage':{'status':'reported','tokens':{
                'input_tokens':90,'cached_input_tokens':0,'cache_write_input_tokens':0,'output_tokens':10,'reasoning_output_tokens':0,'total_tokens':100}}},
                'evidence':{'thread_start':{'model':request['model'],'reasoning_effort':request['reasoning_effort'],'model_provider':'openai'},'rerouted':False}}
        original_load=runner.load
        def load_with_external_boundary(name,path):
            module=original_load(name,path)
            if path.name=='project_memory_consolidation_experiment.py':
                factory=module.native_phase_adapter
                def adapter(protocol,phase):
                    native=factory(protocol,phase)
                    native.NativeProcessorRunner=MagicMock(return_value=external)
                    return native
                module.native_phase_adapter=adapter
            return module
        with tempfile.TemporaryDirectory(prefix='r23-NONEXPERIMENT-') as directory:
            run=Path(directory)
            protocol_path=run/'synthetic-protocol.json';helper.write(protocol_path,protocol)
            helper.write(run/'run.json',{'budget_run':str(run),'started_at':helper.time.time(),'protocol_sha256':helper.digest(protocol)})
            with patch.object(runner,'load',side_effect=load_with_external_boundary):
                if invalid:
                    with self.assertRaises(ValueError):runner.execute(run,protocol_path)
                else:runner.execute(run,protocol_path)
            if invalid:
                self.assertEqual(len(observed),2)
                self.assertEqual(helper.read(run/'receipts/call-0002-response.json')['output'],outputs[0])
                self.assertTrue((run/'public-preflight/synthetic-r23-offline/stopped.json').exists())
                self.assertFalse((run/'public-preflight/synthetic-r23-offline/summary.json').exists())
                self.assertEqual(helper.usage(helper.ledger_calls(run/'calls.jsonl'))['attempted_calls'],2)
                return
            summary=helper.read(run/'public-preflight/synthetic-r23-offline/summary.json')
            first=helper.read(run/'public-preflight/synthetic-r23-offline/reader-01.json')
            owners=helper.reader_assertion_ownership(first['response']['output'])
            delivered=first['resolved']['answers'][0]['answer']
            for owner in next(iter(owners.values())):
                self.assertEqual(delivered[owner['start']:owner['end']],owner['text'])
                self.assertTrue(owner['citation_refs'])
            self.assertEqual(first['response']['output'],outputs[0])
            self.assertEqual(summary['status'],'passed')
            self.assertEqual(summary['native_usage']['attempted_calls'],10)
            self.assertEqual(len(list((run/'receipts').glob('*-request.json'))),10)
            self.assertEqual(len(list((run/'receipts').glob('*-response.json'))),10)
            self.assertFalse((run/'freeze.json').exists())
        self.assertEqual([r['model'] for r in observed],['gpt-6-astra']+['gpt-6.1-sol']*8+['gpt-6-astra'])

    def test_legacy_v2_public_packets_remain_supported(self):
        self.protocol['reader_evidence_contract']='clause-support-v2'
        self.protocol.pop('reader_fidelity_contract',None)
        prepared=probe.prepare(helper,self.protocol,self.corpus)
        payload=probe.judge_payload(helper,self.protocol,self.corpus,native_readers(prepared,False))
        self.assertEqual(len(payload['cases']),8)
        self.assertNotIn('AUXILIARY_ONLY_SENTINEL',json.dumps(payload))

if __name__=='__main__':unittest.main()
