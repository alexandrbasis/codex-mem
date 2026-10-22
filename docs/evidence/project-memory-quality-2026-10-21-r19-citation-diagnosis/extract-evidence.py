#!/usr/bin/env python3
"""Offline extraction of four immutable R18 cases. No judgments or runtime calls."""
from pathlib import Path
import hashlib
import json
import tarfile
E=Path(__file__).resolve().parent
SOURCE=E.parent/'project-memory-quality-2026-10-21-r18/holdout'
def sha(b): return hashlib.sha256(b).hexdigest()
def main():
    records=json.loads((SOURCE/'per-question.json').read_bytes())['questions']
    with tarfile.open(SOURCE/'fictional-run-evidence.tar.gz') as archive:
        def extract(name):
            b=archive.extractfile(name).read();return json.loads(b),{'member':name,'sha256':sha(b)}
        rows=[json.loads(x) for x in archive.extractfile('calls.jsonl').read().splitlines()]
        cases=[]
        for hid,qid in [('E02','Q2'),('E02','Q4'),('E02','Q6'),('E06','Q3')]:
            r=next(x for x in records if x['history_id']==hid and x['question']['id']==qid)
            a=next(x for x in r['attempts'] if x['variant']=='candidate' and x['run_id']=='primary' and x['repeat']==0)
            part,part_pin=extract(f'candidate/{hid}/reader-0.q-{qid}.json')
            call=part['native_receipt']['source_call_id'];request,request_pin=extract(f'receipts/{call}-request.json');response,response_pin=extract(f'receipts/{call}-response.json')
            prefix=request['prompt'].split('\n{"contexts":',1)[0]
            judge_start=next(x for x in rows if x['status']=='started' and x['phase']=='judge' and x['variant']=='candidate' and x['history_id']==hid and x.get('repeat')==0)
            judge,jpin=extract(f"receipts/{judge_start['call_id']}-request.json")
            jprefix=judge['prompt'].split('\n{"original_history":',1)[0]
            cases.append({'key':hid+'/'+qid,'question':r['question'],'gold_required_facts':r['gold']['required_facts'],'delivered_answer':a['answer'],'unchanged_grade':a['grade'],'auxiliary_evidence_checks_not_delivered_answer':part['evidence_checks'][qid],'raw_model_output':response['output'],'reader_prompt':prefix,'reader_output_schema':request['output_schema'],'judge_prompt':jprefix,'context_sha256':a['context']['context_sha256'],'pins':{'reader_part':part_pin,'reader_request':request_pin,'reader_response':response_pin,'judge_request':jpin}})
        result={'schema_version':1,'scope':'Exact offline extraction only. Not a regrade, grade repair or model validation.','source_pins':{n:sha((SOURCE/n).read_bytes()) for n in ('per-question.json','fictional-run-evidence.tar.gz','report.json')},'extractor_sha256':sha(Path(__file__).read_bytes()),'cases':cases}
        target=E/'extracted-evidence.json';target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(sha(target.read_bytes()))
if __name__=='__main__':main()
