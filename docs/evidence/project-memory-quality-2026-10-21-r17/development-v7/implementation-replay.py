import sys,json,hashlib
from pathlib import Path
from copy import deepcopy
from xml.etree import ElementTree as ET
ROOT=Path.cwd();sys.path.insert(0,str(ROOT))
from codex_mem import project_memory_retrieval as r
base=ROOT/'docs/evidence/project-memory-quality-2026-10-21-r17'
# Publicly disclosed DTO diagnostics only. No answer, grading, or sealed inputs.
data=json.loads((base/'frozen-all96-pre-render-dtos.json').read_text())
previous=json.loads((base/'development-v6-design/bounded-monotonic-refill-counterfactual.json').read_text())
scope={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'codex_mem').glob('*.py')}
summary={'owners':0,'v6_sources':0,'lost_v6_sources':0,'v6_associations_retained':0,'budgets_valid':0,'counterfactual_wire_equal':0}
rows=[]
def sources(xml):
 return [{**s.attrib,'quote':s.findtext('quote_excerpt')} for s in ET.fromstring(xml).iter('source') if s.find('quote_excerpt') is not None]
def covered(p,after):
 return any(all(p.get(k)==q.get(k) for k in ['source_id','source_role','source_field','tool_use_id']) and int(q['start'])<=int(p['start']) and int(q['end'])>=int(p['end']) and q['quote'][int(p['start'])-int(q['start']):int(p['end'])-int(q['start'])]==p['quote'] for q in after)
for case,prior in zip(data['cases'],previous['views']):
 for owner,expected in zip(case['owners'],prior['owners']):
  brief=deepcopy(owner['pre_render_brief']);budget=owner['budget']
  canonical=r._render_project_brief(brief,budget)[0];actual=r.render_project_brief(brief,budget)
  old=sources(canonical);new=sources(actual);loss=[p for p in old if not covered(p,new)]
  assoc=lambda xml:[ET.tostring(i) for i in ET.fromstring(xml).findall('item') if i.get('lane') in {'episode','claim','signal'}]
  sameassoc=assoc(canonical)==assoc(actual)
  assert not loss and sameassoc and len(actual)<=budget
  summary['owners']+=1;summary['v6_sources']+=len(old);summary['lost_v6_sources']+=len(loss);summary['v6_associations_retained']+=sameassoc;summary['budgets_valid']+=len(actual)<=budget;summary['counterfactual_wire_equal']+=actual==expected['wire']
  rows.append({'case':case['case'],'project_key':owner['project_key'],'budget':budget,'canonical_chars':len(canonical),'actual_chars':len(actual),'lost_sources':loss,'association_parity':sameassoc,'counterfactual_equal':actual==expected['wire'],'wire':actual})
assert scope=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'codex_mem').glob('*.py')}
out={'status':'pure_implementation_replay_no_quality_claim','model_calls':0,'summary':summary,'source_hashes':scope,'owners':rows}
p=base/'development-v7/implementation-replay.json';p.write_text(json.dumps(out,indent=2)+'\n');print(summary);print(hashlib.sha256(p.read_bytes()).hexdigest())
