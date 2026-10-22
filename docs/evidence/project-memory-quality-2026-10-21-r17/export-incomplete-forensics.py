#!/usr/bin/env python3
"""Offline R17 forensic preservation. Never calls canonical aggregate/export or models.

Adapted per-question and member allowlist structure from frozen helper lines
2108-2218. Missing/ungraded slots are observable stages, never quality failures.
Only unchanged pure helper ledger/usage, repeat and sanitation/archive functions
are invoked. The frozen historical pointer is preserved, not normalized.
"""
from pathlib import Path
import collections
from decimal import Decimal
import hashlib
import importlib.util
import inspect
import json
import sys

ROOT = Path(__file__).resolve().parents[3]
E = Path(__file__).resolve().parent
RUN = Path('/private/tmp/codex-mem-quality-20261021-r17-experiment')
OUT = E / 'incomplete-forensic-export'
HELPER_SHA = 'd935fe07687571bf42271e4a9606c9a98a75a162beb8a050f4b7fcc95960a388'
def sha(b): return hashlib.sha256(b).hexdigest()
def read(p): return json.loads(p.read_bytes())
def encoded(v): return (json.dumps(v,ensure_ascii=False,indent=2,sort_keys=True)+'\n').encode()
def save(p,b):
    p.parent.mkdir(parents=True,exist_ok=True)
    if p.exists() and p.read_bytes()!=b: raise ValueError('Existing forensic output differs: '+str(p.name))
    p.write_bytes(b)

def main():
    helper = E/'frozen-helper.py'
    assert sha(helper.read_bytes()) == HELPER_SHA
    sys.path.insert(0,str(ROOT))
    spec=importlib.util.spec_from_file_location('r17_frozen_forensic_helper',helper)
    h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
    # Required owning evidence is read before preserved run content.
    evidence_names=['external-stop.json','historical-pointer-incompatibility-reproduction.json','historical-pointer-contract-audit.json']
    evidence={n:read(E/n) for n in evidence_names}
    protocol=read(E/'protocol.json'); pins=read(RUN/'inputs.json')
    assert 'report' not in protocol['historical_spend'] and 'path' in protocol['historical_spend']
    originals={}
    def add(name,p):
        if p.is_symlink() or not p.is_file(): raise ValueError('Missing or symlink source '+name)
        originals[name]=p.read_bytes()
    # Exact pinned fixture/gold, no rewriting or validation-driven repair.
    inputs={}
    for path,expected in sorted(pins.items()):
        p=Path(path); assert sha(p.read_bytes())==expected
        name='inputs/'+p.name; add(name,p);inputs[p.name]=read(p)
    fixture=inputs['holdout-fixture.json']; gold=inputs['holdout-gold.json']
    rows=h.ledger_calls(RUN/'calls.jsonl'); current=h.usage(rows)
    assert current['attempted_calls']==152 and current['finished_calls']==152
    assert current['failed_calls']==current['unresolved_calls']==0
    top={'run.json','freeze.json','inputs.json','model-inventory.json','protocol-current.json','native-request-cache.jsonl','native-request-reuse.jsonl','protocol-revisions.jsonl','runtime-revisions.jsonl','calls.jsonl','budget_stops.jsonl','author-input-validation.json','independent-review-validation.json','external-stop.json'}
    for p in sorted(RUN.rglob('*')):
        if not p.is_file() or p.is_symlink(): continue
        rel=p.relative_to(RUN)
        if any(part in ('data','projects','__pycache__') for part in rel.parts): continue
        accept=(len(rel.parts)==1 and p.name in top) or (rel.parts[0]=='receipts' and p.suffix=='.json') or (rel.parts[0] in ('baseline','candidate','replicates','judge-calibration','public-preflight') and p.suffix in ('.json','.jsonl'))
        if accept: add('run/'+str(rel),p)
    # Exact released provenance and control evidence. Explicit list, no arbitrary paths.
    evid= evidence_names+['protocol.json','actual-freeze.json','frozen-source-archive.json','frozen-product.tar.gz','frozen-helper.py','historical-spend.json','author-formal-release.json','released-author-evidence.tar.gz','released-author-evidence-manifest.json','sealed-author-manifest.json','sealed-input-pins.json','author-input-validation.json','independent-review-validation.json','review-source-manifest.json','review-spec-source.json','review-standards-source.json','trial-start.json','public-preflight-summary.json','public-contract-pack-receipt-v2.json']
    for n in evid: add('provenance/'+n,E/n)
    records=[]; stage=collections.Counter()
    for history in fixture['histories']:
        hid=history['id']
        for question in history['questions']:
            record={'history_id':hid,'question':question,'gold':gold['questions'][hid+'/'+question['id']],'attempts':[]}
            planned=[('primary',0)]
            if hid in protocol['reader_repeat_history_ids']: planned.append(('primary',1))
            if hid in protocol['regeneration_repeat_history_ids']: planned.append(('regeneration-1',0))
            for run_id,repeat in planned:
                base=RUN if run_id=='primary' else RUN/'replicates/regeneration-1'
                for variant in ('baseline','candidate'):
                    directory=base/variant/hid
                    gen=read(directory/'generation.json') if (directory/'generation.json').exists() else None
                    mapping=(gen or {}).get('source_map',{})
                    links=[{'event_id':event['id'],'source_id':mapping.get(event['id']),'session_id':session['id'],'project_key':session.get('project_key'),'source_role':event['type'],'text':event.get('text','')} for session in history['sessions'] for event in session['events']]
                    matches=[]
                    for p in sorted(directory.glob('**/reader-*.json')):
                        payload=read(p)
                        if 'output' not in payload or h.reader_artifact_repeat(p,payload)!=repeat: continue
                        if payload.get('question_id') and payload['question_id']!=question['id']: continue
                        if payload.get('question_id') and (p.parent/f'reader-{repeat}.json').exists(): continue
                        answer=next((a for a in payload.get('output',{}).get('answers',[]) if a['id']==question['id']),None)
                        if answer is not None: matches.append((p,payload,answer))
                    assert len(matches)<=1
                    context=None
                    cp=directory/'contexts.json'
                    if cp.exists(): context=next((c for c in read(cp) if c['id']==question['id']),None)
                    attempt={'run_id':run_id,'repeat':repeat,'variant':variant,'answer':None,'grade':None,'context':context,'source_links':links,'status':'missing_answer_and_grade','generation_metadata_exists':gen is not None}
                    if matches:
                        p,payload,answer=matches[0]
                        assert not p.with_name(p.name.replace('reader-','judge-')).exists()
                        attempt.update(answer=answer,status='answer_present_ungraded',reader_artifact=str(p.relative_to(RUN)),reader_shared_metrics=payload.get('metrics'),reader_shared_latency_ms=payload.get('latency_ms'))
                    stage[(variant,run_id,repeat,attempt['status'])]+=1
                    record['attempts'].append(attempt)
            records.append(record)
    assert len(records)==96 and sum(len(r['attempts']) for r in records)==216
    assert sum(a['answer'] is not None for r in records for a in r['attempts'])==72
    originals['per-question.json']=encoded({'schema_version':1,'scope':'Forensic observations only. Not canonical helper output. Missing/ungraded slots do not constitute a quality score.','questions':records})
    public_ids={r['call_id'] for r in rows if r['status']=='started' and r['variant'] in ('public-reader-probe','calibration')}
    public_usage=h.usage([r for r in rows if r['call_id'] in public_ids])
    assert public_usage['attempted_calls']==8 and public_usage['tokens']['total_tokens']==141210
    anchors=sum(len(f['support']) for q in gold['questions'].values() for f in q['required_facts'])
    assert anchors==368
    latency={'scope':'Summed reported call wall_ms, not elapsed trial wall time. Parallel calls overlap.','total_wall_ms':sum(r.get('wall_ms',0) for r in rows if r['status'] in ('completed','failed')),'calls_with_wall_ms':sum(type(r.get('wall_ms')) in (int,float) for r in rows if r['status'] in ('completed','failed'))}
    prior_path=ROOT/protocol['historical_spend']['path']; assert sha(prior_path.read_bytes())==protocol['historical_spend']['sha256']
    prior=read(prior_path)['usage']; cumulative={k:prior[k]+current[k] for k in ('attempted_calls','finished_calls','failed_calls','unresolved_calls','calls_with_reported_tokens','calls_with_unknown_or_partial_tokens')}
    cumulative.update(tokens={k:prior['tokens'][k]+current['tokens'][k] for k in current['tokens']},token_accounting_complete=prior['token_accounting_complete'] and current['token_accounting_complete'],cost_usd=None,hypothetical_api_equivalent_usd={k:str(Decimal(prior['hypothetical_api_equivalent_usd'][k])+Decimal(current['hypothetical_api_equivalent_usd'][k])) for k in ('standard','fast')})
    start=[r for r in rows if r['status']=='started']; phases=collections.Counter((r['phase'],r['variant']) for r in start)
    accounting={'schema_version':1,'scope':'Explicit forensic arithmetic, not frozen canonical historical_accounting. Public calls included exactly once.','usage':current,'historical_usage':prior,'cumulative_usage':cumulative,'historical_pointer_preserved':protocol['historical_spend'],'actual_billed_usd':None,'pricing_complete':False,'hypothetical_api_equivalents_are_billing':False,'pricing_limits':'API-equivalent scenarios only; actual Codex billing unknown. Jev separate, including earlier unknown transport usage. Coding/research/orchestration costs unknown.','public_usage_included_once':public_usage,'latency':latency,'limits':protocol['limits'],'observed_cap_accounting':{'native_calls_remaining':protocol['limits']['max_calls']-current['attempted_calls'],'reported_tokens_remaining':protocol['limits']['max_observed_tokens']-current['tokens']['total_tokens'],'unknown_usage_calls':current['calls_with_unknown_or_partial_tokens'],'stopped_elapsed_seconds':evidence['external-stop.json']['elapsed_seconds']},'phase_calls':[{'phase':k[0],'variant':k[1],'calls':v} for k,v in sorted(phases.items())]}
    statuses=[{'variant':k[0],'run_id':k[1],'repeat':k[2],'status':k[3],'count':v} for k,v in sorted(stage.items())]
    summary={'schema_version':1,'status':'incomplete_frozen_protocol_consumer_mismatch','canonical_report_and_export_available':False,'forensic_export_only':True,'scored_quality_failure':False,'quality_score':None,'quality_rate':None,'gain':None,'goal_complete':False,'original_clock':read(RUN/'run.json')['started_at'],'limits':protocol['limits'],'planned_question_records':96,'planned_attempt_slots':216,'planned_primary_per_arm':96,'planned_reader_repeat_per_arm':6,'planned_regeneration_repeat_per_arm':6,'main_answers':{'baseline':72,'candidate':0},'main_grades':{'baseline':0,'candidate':0},'stage_counts':statuses,'generation_histories_complete':sum(read(RUN/'baseline'/x['id']/'generation.json').get('status')=='complete' for x in fixture['histories']),'candidate_generation_metadata':sum((RUN/'candidate'/x['id']/'generation.json').exists() for x in fixture['histories']),'required_facts':sum(len(v['required_facts']) for v in gold['questions'].values()),'positive_anchors':anchors,'public_calls_included_once':public_usage['attempted_calls'],'public_tokens':public_usage['tokens']['total_tokens'],'usage':current,'cumulative_usage':cumulative,'reason':'Frozen historical_spend has path/sha256; unchanged helper requires report. Canonical aggregate/export unavailable. No frozen repair or retry.','external_stop':evidence['external-stop.json'],'thresholds_unchanged':protocol['pass_criteria']}
    originals['canonical-report-unavailable.json']=encoded({'canonical_output_exists':False,'reason':summary['reason'],'forensic_export_is_not_canonical_result':True,'quality_score':None})
    cleaned,sanitization=h.sanitize_export_members(originals)
    again,second=h.sanitize_export_members(cleaned)
    assert cleaned==again and second['changed_member_count']==0
    archive=h.export_archive_bytes(cleaned)
    assert archive==h.export_archive_bytes(cleaned)
    for name,data in sorted(cleaned.items()): save(OUT/'members'/name,data)
    save(OUT/'forensic-evidence.tar.gz',archive)
    sanitization.update(private_original_archive_sha256=sha(h.export_archive_bytes(originals)),public_export_archive_sha256=sha(archive),second_pass_changed_member_count=0,idempotence_pass=True)
    save(OUT/'sanitization.json',encoded(sanitization))
    provenance={'helper_sha256':HELPER_SHA,'borrowed_structure_source_lines':[2108,2218],'pure_helper_functions':{n:{'sha256':sha(inspect.getsource(getattr(h,n)).encode())} for n in ('usage','ledger_calls','sanitize_export_members','export_archive_bytes','reader_artifact_repeat')},'canonical_functions_never_called':['aggregate','export_evidence','historical_accounting'],'exporter_sha256':sha(Path(__file__).read_bytes())}
    save(OUT/'exporter-provenance.json',encoded(provenance))
    manifest={'schema_version':1,'scope':'Incomplete forensic export only; not canonical helper result or salvaged comparison','question_records':96,'attempt_slots':216,'archive_member_count':len(cleaned),'archive_sha256':sha(archive),'members':{n:{'public_sha256':sha(cleaned[n]),'private_original_sha256':sha(originals[n]),'bytes':len(cleaned[n])} for n in sorted(cleaned)},'files':{str(p.relative_to(OUT)):sha(p.read_bytes()) for p in sorted(OUT.rglob('*')) if p.is_file() and p.name!='export-manifest.json'},'exclusions':['SQLite','data/configuration','auth','caches','locks','private process control','unlisted arbitrary paths'],'sanitization_idempotent':True,'source_inputs_exact':pins,'usage':current}
    save(OUT/'export-manifest.json',encoded(manifest))
    save(E/'accounting.json',encoded(accounting));save(E/'result-summary.json',encoded(summary))
    print(json.dumps({'status':summary['status'],'usage':current,'cumulative':cumulative,'members':len(cleaned),'stage_counts':statuses,'hashes':{str(p.relative_to(E)):sha(p.read_bytes()) for p in (Path(__file__),OUT/'export-manifest.json',E/'accounting.json',E/'result-summary.json')}},indent=2))
if __name__=='__main__': main()
