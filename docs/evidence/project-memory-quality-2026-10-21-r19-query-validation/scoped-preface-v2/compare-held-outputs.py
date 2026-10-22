#!/usr/bin/env python3
"""Pure saved-output comparison; never opens Store or executes retrieval."""
import hashlib,json,re
from pathlib import Path
B=Path(__file__).resolve().parent; P=B.parent; ROOT=B.parents[3]
def read(p):return json.loads(p.read_bytes())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def rows(p):return {(r['history_id'],r['question_id']):r for r in read(p)['records']}
def cov(r,mirror=False):
 out={}
 for a in r['anchor_checks']:
  exact=a['fully_visible_in_one_grounded_span']
  other=any(s['source_id']==a['source_id'] and s['source_field']!=a['source_field'] and a['quote'] in s['quote'] for s in r['grounded_quote_catalog'])
  out[(a['fact_index'],a['support_index'])]=exact or (mirror and other)
 return out
def facts(r,mirror=False):
 c=cov(r,mirror);return {i:all(v for (f,s),v in c.items() if f==i) for i in {f for f,s in c}}
old=rows(B/'old-r18-frozen-contexts.json');first=rows(P/'new-held-query-contexts.json');new=rows(B/'new-held-query-contexts.json');assert set(old)==set(first)==set(new)
comparisons={}
for label,before in [('r18',old),('first_120b',first)]:
 stats={};details={}
 for unit,func in [('strict_anchor',cov),('strict_fact',facts),('same_source_mirror_fact',lambda r:facts(r,True))]:
  gain=[];loss=[]
  for k in before:
   bc,ac=func(before[k]),func(new[k]);assert bc.keys()==ac.keys()
   for key in bc:
    if bc[key]!=ac[key]:(gain if ac[key] else loss).append({'case':k,'unit':key})
  stats[unit]={'before':sum(sum(func(r).values()) for r in before.values()),'after':sum(sum(func(r).values()) for r in new.values()),'gains':len(gain),'losses':len(loss)};details[unit]={'gains':gain,'losses':loss}
 span_loss=[]
 for k in before:
  for s in before[k]['grounded_quote_catalog']:
   if not any(n['source_id']==s['source_id'] and n['source_field']==s['source_field'] and n['start']<=s['start'] and n['end']>=s['end'] and n['quote'][s['start']-n['start']:s['end']-n['start']]==s['quote'] for n in new[k]['grounded_quote_catalog']):span_loss.append({'case':k,'span':s})
 comparisons[label]={'metrics':stats,'changes':details,'previous_grounded_spans_not_contained_in_new':span_loss}
r=read(B/'replay-receipt.json');prior=read(P/'post-replay-integrity.json')
first_unchanged={n:sha(P/n)==v for n,v in prior['new_artifact_sha256'].items()}
first_unchanged['replay-contexts.py']=sha(P/'replay-contexts.py')==prior['source_script_sha256']
closed=ROOT/'docs/evidence/project-memory-quality-2026-10-21-r18/closing-bundle.tar.gz'
assert sha(closed)=='a0545ee637952c8440e4b37961ba9ba6dd6440a83ab2c017480b625414179ded'
patterns={'private_key':r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----','openai_key':r'\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}','aws_key':r'\bAKIA[0-9A-Z]{16}\b','jwt':r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+','access_url':r'https?://[^\s"<>]*(?:access_token=|api_key=|token=|signature=)'}
files=[p for p in B.iterdir() if p.is_file()];texts='\n'.join(p.read_text() for p in files)
privacy={k:len(re.findall(v,texts)) for k,v in patterns.items()}
# Only actual native diagnostic UUID values, never fictional source IDs.
run=Path('/private/tmp/codex-mem-quality-20261021-r18-experiment');ids=set()
def visit(v,key=''):
 if isinstance(v,dict):
  for k,x in v.items():visit(x,k)
 elif isinstance(v,list):
  for x in v:visit(x,key)
 elif isinstance(v,str) and any(s in key.lower() for s in ('worker','thread','turn')):
  ids.update(re.findall(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b',v,re.I))
for line in (run/'calls.jsonl').read_text().splitlines():visit(json.loads(line))
raw_hits=[v for v in sorted(ids) if v in texts]
# Scan serialized artifacts, excluding this scanner's literal test patterns.
artifact_text='\n'.join(p.read_text() for p in files if p.suffix=='.json')
privacy={k:len(re.findall(v,artifact_text)) for k,v in patterns.items()}
old_first_xml_equal=sum(old[k]['context']['memory']==rows(P/'old-r18-frozen-contexts.json')[k]['context']['memory'] for k in old)
result={'status':'complete_disclosed_offline_comparison','comparisons':comparisons,'remaining_empty_cases':[k for k,v in new.items() if not v['grounded_quote_catalog']], 'old_replay_xml_identical_to_first_old':old_first_xml_equal,'all_first_replay_files_preserved':first_unchanged,'r18_closed_bundle_sha256':sha(closed),'original_data_and_evidence_unchanged':r['original_data_and_evidence_unchanged'],'all_48_runtime_pins_unchanged':r['current_runtime_sources_unchanged_during_replay'],'runtime_hashes':r['new_runtime_hashes'],'privacy_pattern_counts':privacy,'actual_native_diagnostic_uuid_hits':raw_hits,'artifact_sha256':{p.name:sha(p) for p in files},'additional_store_retrieval_model_network_calls':0,'limits':['Disclosed source coverage only; no fresh model quality or grade changes.','Mirror facts permit exact text in another field of the same canonical source; strict facts require original field and offsets.','Raw-context and annotated-memory caps are independently recorded; no sum-of-six-context cap claim.']}
assert all(first_unchanged.values()) and not raw_hits and not any(privacy.values()) and old_first_xml_equal==96
out=B/'comparison-integrity.json';assert not out.exists();out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'sha256':sha(out),'comparisons':{k:v['metrics'] for k,v in comparisons.items()},'span_losses':{k:len(v['previous_grounded_spans_not_contained_in_new']) for k,v in comparisons.items()},'privacy':privacy}))
