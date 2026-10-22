#!/usr/bin/env python3
"""Read-only extraction of frozen primary R19 semantic witnesses, no Store/model."""
from pathlib import Path
import hashlib
import ast
import json
import re
import xml.etree.ElementTree as ET
E=Path(__file__).resolve().parent
ROOT=E.parents[2]
RUN=Path('/private/tmp/codex-mem-quality-20261021-r19-experiment')
def sha(b):return hashlib.sha256(b).hexdigest()
def read(p):return json.loads(p.read_bytes())
def pin(p):return {'path':str(p),'sha256':sha(p.read_bytes())}
def main():
    inputs=read(RUN/'inputs.json');values={}
    for path,expected in inputs.items():
        p=Path(path);assert sha(p.read_bytes())==expected;values[p.name]=read(p)
    fixture=values['holdout-fixture.json'];gold=values['holdout-gold.json'];cases=[]
    for hid,qid in [('R19H12','q6'),('R19H11','q3')]:
        history=next(x for x in fixture['histories'] if x['id']==hid)
        question=next(x for x in history['questions'] if x['id']==qid)
        row={'key':hid+'/'+qid,'question':question,'gold':gold['questions'][hid+'/'+qid],'original_history':history['sessions'],'variants':{}}
        for role in ('baseline','candidate'):
            directory=RUN/role/hid;partpath=directory/f'reader-0.q-{qid}.json';part=read(partpath)
            gen=read(directory/'generation.json');contexts=read(directory/'contexts.json');context=next(x for x in contexts if x['id']==qid)
            gradepath=directory/'judge-0.json';judge=read(gradepath);grade=next(x for x in judge['grades'] if x['id']==qid)
            call=part['native_receipt']['source_call_id'];reqpath=RUN/'receipts'/f'{call}-request.json';respath=RUN/'receipts'/f'{call}-response.json';request=read(reqpath);response=read(respath)
            raw=response['output']['answers'][qid];answer=part['output']['answers'][0];ownership=part['assertion_ownership'][qid]
            assert answer['answer']==(' '.join(a['text'] for a in raw['assertions']) if raw['assertions'] else 'Unknown from supplied memory.')
            assert len(ownership)==len(raw['assertions'])
            for owner,assertion in zip(ownership,raw['assertions']):
                assert answer['answer'][owner['start']:owner['end']]==assertion['text']==owner['text']
                assert owner['citation_refs']==assertion['citation_refs']
            assert [c['catalog_ref'] for c in answer['citations']]==list(dict.fromkeys(ref for a in raw['assertions'] for ref in a['citation_refs']))
            events={gen['source_map'][ev['id']]:{'event':ev,'session_id':session['id'],'project_key':session['project_key']} for session in history['sessions'] for ev in session['events'] if ev['id'] in gen['source_map']}
            citations=[]
            for citation in answer['citations']:
                origin=events[citation['source_id']];event=origin['event'];field=citation['source_field']
                if field=='body':body=event.get('text','')
                else:
                    container,name=field.split('.',1);body=event['tool_capture'][container][name]
                assert body[citation['start']:citation['end']]==citation['quote']
                expected_role={'user':'user_intent','assistant':'assistant_report','tool':'tool_record'}[event['type']]
                assert citation['source_role']==expected_role
                citations.append({'delivered_citation':citation,'origin':origin,'owner_indices':[i for i,x in enumerate(ownership) if citation['catalog_ref'] in x['citation_refs']],'canonical_scope':'Explicit event/session/project attribution only; no environment/version/current applicability inferred.'})
            fields=[]
            for episode in gen['persisted_state']['episodes']:
                sr=episode['stored_record'];fields.append({'episode_id':episode['id'],'scope':sr.get('scope'),'continuity_basis':sr.get('continuity_basis'),'stored_fields':{k:v for k,v in sr.items() if k in ('goal','summary','rationale','open_work','outcome','evidence','field_provenance','state')}})
            items=[]
            for block in re.findall(r'<project-memory\b.*?</project-memory>',context['memory'],re.S):
                for item in ET.fromstring(block).findall('item'):
                    items.append({'attributes':item.attrib,'fields':[f.attrib for f in item.findall('field')],'source_pointers':[s.attrib for s in item.iter('source')]})
            rawjudge=judge.get('native_dispatch',{}).get('source_call_id');jprompt=None
            if rawjudge:
                jreq=read(RUN/'receipts'/f'{rawjudge}-request.json');jprompt=jreq['prompt'].split('\n{"original_history":',1)[0]
            row['variants'][role]={'delivered_answer':answer,'raw_assertions':raw['assertions'],'raw_auxiliary_checks_no_credit':raw['evidence_check'],'ownership_verified':ownership,'all_delivered_citations_with_origins':citations,'context':context,'context_items':items,'grade_unchanged':grade,'reader_prompt':request['prompt'].split('\n{"contexts":',1)[0],'reader_output_schema':request['output_schema'],'judge_prompt':jprompt,'generation_state_sha256':gen['state_sha256'],'stored_representation':fields,'pins':{x:pin(y) for x,y in [('reader_part',partpath),('reader_request',reqpath),('reader_response',respath),('judge',gradepath),('contexts',directory/'contexts.json'),('generation',directory/'generation.json')]}}
        cases.append(row)
    supporting_baseline_grades = read(RUN/'baseline/R19H12/judge-0.json')['grades']
    counterexamples = [{'question_id':g['id'],'grade':g,
                       'answer':next(a for a in read(RUN/'baseline/R19H12/reader-0.json')['output']['answers'] if a['id']==g['id'])}
                      for g in supporting_baseline_grades if g.get('critical_errors')]
    helper=E.parent/'project-memory-quality-2026-10-21-r19/frozen-helper.py'
    assert sha(helper.read_bytes())==read(RUN/'freeze.json')['helper_sha256']
    helper_functions={n.name:{'start_line':n.lineno,'end_line':n.end_lineno} for n in ast.parse(helper.read_text()).body if isinstance(n,ast.FunctionDef) and n.name in ('resolve_assertion_reader_output','reader_assertion_ownership','assertion_reader_request','reader_answer_for_judge')}
    output={'frozen_helper':{**pin(helper),'functions':helper_functions},'baseline_same_history_critical_counterexamples':counterexamples,'schema_version':1,'scope':'Frozen primary-case extraction only, no regrade, semantic correction, full comparison or regeneration observation.','source_pins':inputs,'formal_release':pin(E.parent/'project-memory-quality-2026-10-21-r19/formal-release.json'),'script_sha256':sha(Path(__file__).read_bytes()),'cases':cases,'interpretation':{'R19H12/q6':'All required scan facts are recalled in both arms. Candidate adds affirmative causal relation in delivered owned assertion. Whole-source citations validate address, not that causal relation. Judge coarse recall=false is not an additional missing scan fact.','R19H11/q3':'One required assistant-report fact is absent from candidate metadata-only context and its delivered unknown branch; baseline attributes visible report. Query/source-cause investigation belongs to separate source worker.','counterevidence':['Authorship and pending publication occur in one user paragraph, making causal interpretation pragmatically plausible, but no explicit link is written.','Baseline q6 lists those facts without because and is accepted. Other baseline H12 questions make the same causal inference and are independently flagged.','Stored rationale associates adjacent statements but adds no causal connective; candidate transport shows whole original with unresolved custody and no requirement-claim assertion.','No independent verified world truth or judge correctness measurement is established by this diagnostic.'],'restrictions':{'native_or_network_calls':0,'Store_calls':0,'source_or_protocol_or_grade_changes':False}}}
    target=E/'semantic-evidence.json';target.write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n');print(sha(target.read_bytes()))
if __name__=='__main__':main()
