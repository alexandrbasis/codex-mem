"""Join released source/grade witnesses without judging or modifying any answer."""
import sys,json,re,hashlib,importlib.util
from pathlib import Path
ROOT=Path('/Users/alexandrbasis/Desktop/Coding/codex-mem-public');sys.dont_write_bytecode=True;sys.path.insert(0,str(ROOT))
RUN=Path('/private/tmp/codex-mem-quality-20261021-r19-experiment');OUT=ROOT/'docs/evidence/project-memory-quality-2026-10-21-r19-source-diagnosis';R19=ROOT/'docs/evidence/project-memory-quality-2026-10-21-r19';SEALED=Path('/private/tmp/codex-mem-quality-20261021-r19-sealed')
h=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
freeze=json.load(open(RUN/'freeze.json'));before={str(p.relative_to(ROOT)):h(p) for p in (ROOT/'codex_mem').glob('*.py')};assert before==freeze['candidate_hashes']
fixture=json.load(open(SEALED/'holdout-fixture.json'));histories={x['id']:x for x in fixture['histories']};gold=json.load(open(SEALED/'holdout-gold.json'))['questions']
spec=importlib.util.spec_from_file_location('r19_frozen_helper_diagnosis',R19/'frozen-helper.py');helper=importlib.util.module_from_spec(spec);sys.modules[spec.name]=helper;spec.loader.exec_module(helper);assert h(R19/'frozen-helper.py')==freeze['helper_sha256']
traces={a:json.load(open(OUT/(a+'-trace.json'))) for a in ['baseline','candidate']};byarm={a:{(r['case'],r['id']):r for r in traces[a]['rows']} for a in traces}
grades={};contexts={};answers={};empty={};pins={}
for arm in ['baseline','candidate']:
 empty[arm]=[]
 for directory in sorted((RUN/arm).glob('R19H*')):
  case=directory.name
  for name in ['contexts.json','reader-0.json','judge-0.json','generation.json','citation-catalog.json']:
   pins[str((directory/name).relative_to(RUN))]=h(directory/name)
  for row in json.load(open(directory/'contexts.json')):
   contexts[arm,case,row['id']]=row
   if '<quote_excerpt>' not in row['memory']:empty[arm].append(case+'/'+row['id'])
  for row in json.load(open(directory/'judge-0.json'))['grades']:grades[arm,case,row['id']]=row
  for row in json.load(open(directory/'reader-0.json'))['output']['answers']:answers[arm,case,row['id']]=row
paired=[];coarse=[]
for arm,case,qid in list(grades):
 if arm!='candidate':continue
 b,c=grades['baseline',case,qid],grades['candidate',case,qid]
 facts={f['fact_index']:f for f in b['fact_recall']}
 for f in c['fact_recall']:
  if facts[f['fact_index']]['recalled'] and not f['recalled']:paired.append({'case':case,'id':qid,'fact_index':f['fact_index'],'baseline_witness':facts[f['fact_index']],'candidate_witness':f})
 if b['constraints_obligations_reasons_recalled'] and not c['constraints_obligations_reasons_recalled']:
  coarse.append({'case':case,'id':qid,'baseline_fact_count':b['required_facts_recalled'],'candidate_fact_count':c['required_facts_recalled'],'baseline_rationale':b['rationale'],'candidate_rationale':c['rationale'],'semantic_analysis_deferred':case+'/'+qid!='R19H11/q3'})
roles={'user':'user_intent','assistant':'assistant_report','tool':'tool_record'}
views=[]
for key in sorted(byarm['candidate']):
 case,qid=key;history=histories[case];events={e['id']:e for s in history['sessions'] for e in s['events']};armrows={}
 for arm in ['baseline','candidate']:
  trace=byarm[arm][key];context=contexts[arm,case,qid];originals=helper.original_source_fields(history,trace['generation_source_map']);catalog=helper.reader_citation_catalog([context],originals)
  perquestion=json.load(open(RUN/arm/case/('reader-0.q-'+qid+'.json')));assert helper.digest(catalog)==perquestion['catalog_sha256']
  entries={e['id']:e for e in trace['canonical_entries']};facts=[]
  for fi,fact in enumerate(gold[case+'/'+qid]['required_facts']):
   supports=[]
   for support in fact['support']:
    sid=trace['generation_source_map'].get(support['event_id']);entry=entries.get(sid);event=events[support['event_id']];role=roles[event['type']];body=entry.get('body') if entry and support['source_field']=='body' else None
    stored_exact=bool(body is not None and body[support['start']:support['end']]==support['quote'] and
                      entry['source'].startswith({'user_intent':'hook:UserPromptSubmit','assistant_report':'hook:Stop','tool_record':'hook:PostToolUse'}[role]))
    def covers(c,mirror=False):
     return (c['source_id']==sid and c['source_role']==role and (mirror or c['source_field']==support['source_field']) and c['start']<=support['start']<support['end']<=c['end'] and c['quote'][support['start']-c['start']:support['end']-c['start']]==support['quote'])
    supports.append({**support,'source_id':sid,'source_role':role,'canonical_entry_present':entry is not None,'canonical_body_exact':stored_exact,'stored_project':entry.get('project') if entry else None,'body_excerpt':body,'visible_exact_field':any(covers(c) for c in catalog),'visible_same_source_text_any_field':any(covers(c,True) for c in catalog)})
   facts.append({'fact_index':fi,'text':fact['text'],'support':supports})
  armrows[arm]={'database_sha256_before':trace['database_sha256_before'],'database_sha256_after':trace['database_sha256_after'],'context':context,'plan':trace['owners'][0]['plan'],'skip_reason':trace['owners'][0]['brief'].get('skip_reason'),'admitted_records':len(trace['owners'][0]['brief']['records']),'unassociated_user_groups':len(trace['owners'][0]['brief'].get('unassociated_user_context') or []),'unassociated_role_groups':len(trace['owners'][0]['brief'].get('unassociated_role_context') or []),'source_evidence_coverage':trace['owners'][0]['brief'].get('source_evidence_coverage'),'original_role_context_coverage':trace['owners'][0]['brief'].get('original_source_context_coverage'),'catalog':catalog,'catalog_receipt_sha256':perquestion['catalog_sha256'],'raw_xml_chars':sum(len(x) for x in re.findall(r'<project-memory\b.*?</project-memory>',context['memory'],re.S)),'owner_budget':5900,'snapshot_calls':len(trace['owners'][0]['trace']['snapshot_calls']),'source_pool_calls':len(trace['owners'][0]['trace']['source_pool_calls']),'saved_grade':grades[arm,case,qid],'saved_answer':answers[arm,case,qid],'facts_source_chain':facts,'replay_byteexact':trace['replay_byteexact']}
 if armrows['candidate']['skip_reason']=='historical_query':classification='pre_acquisition_historical_guard_shared_by_both_arms'
 elif not armrows['candidate']['catalog']:classification='pre_render_hard_identifier_eligibility_regression'
 elif armrows['baseline']['skip_reason']=='historical_query':classification='candidate_source_delivery_gain_from_existing_current_evidence_routing'
 else:classification='candidate_source_delivery_gain_from_bounded_neutral_originals'
 views.append({'case':case,'id':qid,'question':contexts['candidate',case,qid]['question'],'classification':classification,'arms':armrows})
from codex_mem.project_memory_query import plan_query
loss=next(v for v in views if v['case']=='R19H11' and v['id']=='q3');entry=next(e for e in byarm['candidate']['R19H11','q3']['canonical_entries'] if e['id']=='767a238c3abf4e4d82232f99dd0c526e');query=loss['question'];owner=loss['arms']['candidate']['context']['selected_scope_context']['selected_project_scopes'][0]
value={'text':entry['body'],'source_role':'assistant_report','source_id':entry['id']}
original_plan=plan_query(query,owner,selected_project_scopes=[owner]);variant_plan=plan_query(query.replace('public-cut','public cut'),owner,selected_project_scopes=[owner])
probe={'status':'pure_plan_only_nontrial_word_boundary_counterfactual','original_query':query,'variant_query':query.replace('public-cut','public cut'),'same_unchanged_source_body':entry['body'],'original_identifiers':original_plan.identifiers,'original_matches':original_plan.matches(value),'variant_identifiers':variant_plan.identifiers,'variant_matches':variant_plan.matches(value),'quality_claim':False,'counterfactual_context_not_generated':True}
assert before=={str(p.relative_to(ROOT)):h(p) for p in (ROOT/'codex_mem').glob('*.py')}
report={'schema_version':1,'scope':'released R19 primary source-chain diagnosis only; no regrade or rescore','formal_release_sha256':h(R19/'formal-release.json'),'fixture_sha256':h(SEALED/'holdout-fixture.json'),'gold_sha256':h(SEALED/'holdout-gold.json'),'frozen_query_sha256':before['codex_mem/project_memory_query.py'],'frozen_helper_sha256':h(R19/'frozen-helper.py'),'runtime48_unchanged':True,'runtime_hashes':before,'input_pins':pins,'paired_fact_losses':paired,'coarse_constraint_recall_losses':coarse,'empty_views':empty,'query_boundary_probe':probe,'views':views,'limits':['Frozen judge/reader outcomes are copied as observations; no answer or source is regraded.','Catalog exact-span visibility is mechanical; visibility alone does not establish semantic support. Golden interval absence is not proof that no sufficient paraphrase exists.','The changed-query plan probe demonstrates one literal-matching boundary only. It is not a fixed context, score, or future quality claim.','Original databases were opened mode=ro for SQLite backups/reads. Store construction and replay occurred only on disposable backup databases.','Eight union-empty views replayed byteexact in each arm. No full 96-context rerun was requested or performed. No model, native or network call was made.']}
path=OUT/'source-chain-diagnosis.json';path.write_text(json.dumps(report,indent=2)+'\n');print('paired',len(paired),'empty', {a:len(x) for a,x in empty.items()},'views',len(views),'sha256',h(path));print('probe',probe)
