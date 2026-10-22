#!/usr/bin/env python3
"""Offline observations from immutable R18 export; no Store, models or regrading."""
import collections
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tarfile

E=Path(__file__).resolve().parent
ROOT=E.parents[2]
def sha(data): return hashlib.sha256(data).hexdigest()
def read(p): return json.loads(p.read_bytes())
def main():
    held=E/'holdout';protocol=read(held/'protocol.json');fixture=read(held/'fixture.json');questions=read(held/'per-question.json')['questions']
    helper=E/'frozen-helper.py';assert sha(helper.read_bytes())=='d935fe07687571bf42271e4a9606c9a98a75a162beb8a050f4b7fcc95960a388'
    sys.path.insert(0,str(ROOT));spec=importlib.util.spec_from_file_location('r18_diagnostic_helper',helper);h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
    with tarfile.open(held/'fictional-run-evidence.tar.gz') as archive:
        def member(n): return json.load(archive.extractfile(n))
        histories={x['id']:x for x in fixture['histories']}
        generations={hid:member('baseline/'+hid+'/generation.json') for hid in histories}
        generation_rows=[]
        for hid,g in generations.items():
            candidate=member('candidate/'+hid+'/generation.json');audit=member('candidate/'+hid+'/consolidation.json')
            generation_rows.append({'history_id':hid,'status':g['status'],'source_count':len(g['source_map']),'stored_episodes':len(g['persisted_state']['episodes']),'stored_heads':len(g['persisted_state']['heads']),'cloned_state_matches':candidate['state_sha256']==g['state_sha256'],'candidate_consolidation_metadata':audit})
        # Reconstruct only pure query plans under their exact isolated runtimes.
        for path,expected in protocol['baseline_runtime_hashes'].items(): assert sha((Path(protocol['baseline_runtime_root'])/path).read_bytes())==expected
        frozen=read(E/'frozen-source-archive.json')['candidate_runtime_files']
        for path in ('codex_mem/project_memory_query.py','codex_mem/project_scopes.py'):
            assert sha((ROOT/path).read_bytes())==frozen[path]
        planner_code="""import dataclasses,inspect,json,sys
from pathlib import Path
from codex_mem.project_memory_query import plan_query
v=json.loads(Path(sys.argv[1]).read_text()); result={}
for history in v['histories']:
 for q in history['questions']:
  selected=q.get('project_keys',[q['project_key']] if 'project_key' in q else list({s.get('project_key','default') for s in history['sessions']}))
  projects={s.get('project_key','default'):str(Path(sys.argv[2])/'projects'/history['id']/s.get('project_key','default')) for s in history['sessions']}
  scopes=[projects[k] for k in selected]
  params={'selected_project_scopes':scopes} if 'selected_project_scopes' in inspect.signature(plan_query).parameters else {}
  plan=dataclasses.asdict(plan_query(q['question'],scopes[0],**params))
  result[history['id']+'/'+q['id']]={k:plan.get(k) for k in ('natural','identifiers','task_ids','versions','candidate_queries','scope_aliases','answer_subject','projection')}
print(json.dumps(result,default=str))
"""
        plans={}
        for role,runtime in [('baseline',protocol['baseline_runtime_root']),('candidate',str(ROOT))]:
            code='import sys;sys.path.insert(0,'+repr(runtime)+');'+planner_code
            plans[role]=json.loads(subprocess.check_output([sys.executable,'-c',code,str(held/'fixture.json'),'/private/tmp/codex-mem-quality-20261021-r18-experiment'],cwd=ROOT))
        result=[]
        summary={role:collections.Counter() for role in ('baseline','candidate')}
        for record in questions:
            hid=record['history_id'];qid=record['question']['id'];key=hid+'/'+qid;g=generations[hid]
            originals=h.original_source_fields(histories[hid],g['source_map'])
            row={'key':key,'question':record['question'],'plans':{role:plans[role][key] for role in plans},'variants':{}}
            for role in summary:
                attempt=next(a for a in record['attempts'] if a['variant']==role and a['run_id']=='primary' and a['repeat']==0)
                context=attempt['context'];catalog=h.reader_citation_catalog([context],originals);grade=attempt['grade'];counts=summary[role]
                counts['questions']+=1;counts['context_chars_sum']+=context['context_chars'];counts['displayed_sources']+=len(catalog)
                counts['no_displayed_sources']+=not catalog;counts['graded']+=grade is not None;counts['fully_supported_observed']+=bool(grade and grade['full_supported_answer'])
                counts['empty_or_evasive_observed']+=bool(grade and grade['empty_or_evasive'])
                facts=[]
                for index,fact in enumerate(record['gold']['required_facts']):
                    support=[]
                    for anchor in fact['support']:
                        sid=g['source_map'].get(anchor['event_id']);field=anchor.get('source_field','body');quote=anchor['quote']
                        body=originals.get(sid,{}).get('fields',{}).get(field,'')
                        assert body[anchor['start']:anchor['end']]==quote
                        exact=[x for x in catalog if x['source_id']==sid and x['source_field']==field and x['start']<=anchor['start'] and x['end']>=anchor['end']]
                        mirrors=[x for x in catalog if x['source_id']==sid and quote in x['quote'] and x['source_field']!=field]
                        stored=[{'episode_id':ep['id'],'field':f,'source_role':ev.get('source_role'),'start':ev['start'],'end':ev['end']} for ep in g['persisted_state']['episodes'] for f,refs in ep['stored_record'].get('evidence',{}).items() for ev in refs if ev.get('source_id')==sid and ev['start']<=anchor['start'] and ev['end']>=anchor['end']]
                        support.append({'event_id':anchor['event_id'],'source_id':sid,'source_field':field,'source_role':originals.get(sid,{}).get('source_role'),'start':anchor['start'],'end':anchor['end'],'quote':quote,'stored_episode_evidence':stored,'full_exact_visible':bool(exact),'same_source_field_mirror_visible':bool(mirrors),'visible_entries':exact+mirrors})
                    full=all(x['full_exact_visible'] or x['same_source_field_mirror_visible'] for x in support)
                    recall=next((f['recalled'] for f in grade['fact_recall'] if f['fact_index']==index),None) if grade else None
                    facts.append({'fact_index':index,'text':fact['text'],'supports':support,'all_supports_visible_exact_or_same_source_mirror':full,'fixed_grade_recalled':recall})
                    counts['required_facts']+=1;counts['facts_all_supports_visible']+=full
                    if recall is False: counts['fixed_unrecalled_visible' if full else 'fixed_unrecalled_not_all_visible']+=1
                counts['project_name_content_identifier']+=any(v.casefold() in {p.casefold() for p in g['projects']} for v in plans[role][key]['identifiers'])
                row['variants'][role]={'context_chars':context['context_chars'],'context_sha256':context['context_sha256'],'catalog_count':len(catalog),'facts':facts,'reader_artifact':attempt.get('reader_artifact'),'answer':attempt['answer'],'grade':grade,'context':context}
            result.append(row)
        totals={role:dict(v,context_chars_mean=v['context_chars_sum']/v['questions']) for role,v in summary.items()}
        assert totals['baseline']['graded']==96 and totals['candidate']['graded']==42
        diagnosis={'schema_version':1,'scope':'Secondary offline source-chain diagnosis. No score replacement, regrade, new retrieval, Store or model calls. Primary observations only; repeats remain in canonical report.','source_hashes':{n:sha((held/n).read_bytes()) for n in ('report.json','per-question.json','fixture.json','gold.json','fictional-run-evidence.tar.gz')},'script_sha256':sha(Path(__file__).read_bytes()),'counts':totals,'generations':generation_rows,'questions':result,'interpretation':{'baseline':'Exact frozen baseline planner treats unquoted CamelCase project names as content identifiers. Selected scopes unsupported. Sources and stored episode evidence exist, but all rendered contexts have no valid displayed source. Reader abstention is compatible with its supplied evidence boundary. This is not a producer capture-loss finding.','candidate':'Partial grades only. Full source visibility is measured from per-question exact displayed source fields, with exact same-source mirror distinguished. Visible anchors do not establish semantic correctness, relevance, role inference or truth. Missing full anchors do not alone prove a budget cause.','general_repair_seam':'Maintain trusted scope identity separately from content anchors; assess bounded eligible original transport and source allocation independently of semantic answer quality. Do not add fixture-specific aliases, weaken hard scopes, rewrite frozen baseline or tune disclosed phrases.','unknown_usage_failure':'One runner failure has unavailable usage and no retained reason_code. Account capacity observation is not causal proof. No attempt to recover missing usage or retry.','limits':['Gold prose may require semantic relations not settled by quote availability.','Literal full-anchor visibility is stricter than sufficient semantic support, and different from grade fact recall.','Only saved generation metadata and canonical source records were inspected; no Store was opened or replayed.','Candidate42graded cannot establish full-arm quality.','Earliest capture and adjacent sources do not establish one task or event.']}}
        (E/'source-chain-diagnosis.json').write_text(json.dumps(diagnosis,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'counts':totals,'diagnosis_sha256':sha((E/'source-chain-diagnosis.json').read_bytes())},indent=2))
if __name__=='__main__': main()
