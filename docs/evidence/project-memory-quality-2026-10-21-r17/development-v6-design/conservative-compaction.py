import sys,json,re,hashlib
from pathlib import Path
from copy import deepcopy
from xml.etree import ElementTree as ET
ROOT=Path('/Users/alexandrbasis/Desktop/Coding/codex-mem-public');sys.path.insert(0,str(ROOT))
from codex_mem import project_memory_retrieval as r
D=json.loads((ROOT/'docs/evidence/project-memory-quality-2026-10-21-r17/frozen-all96-pre-render-dtos.json').read_text())
C=json.loads((ROOT/'docs/evidence/project-memory-quality-2026-10-21-r17/development-v5/central-render-assessment/contexts-and-coverage.json').read_text())
original_execution=r._execution_xml; original_source=r._source_xml;original_attrs=r._item_attrs
state={'mode':'baseline','native_groups_compacted':0,'native_serialized_chars_saved':0,'report_pointer_chars_saved':0,'synthetic_item_id_chars_saved':0}
def native_compact(record,projection):
 node=original_execution(record,projection)
 if state['mode']!='conservative' or not node:return node
 ex=record.get('execution') or {}; ps=[p for f in record.get('fields',[]) for p in f.get('evidence',[])]
 if not (len(ps)==2 and all(p.get('source_role')=='tool_record' and p.get('source_id')==record.get('id') and p.get('tool_use_id')==ex.get('tool_use_id') and p.get('start')==0 and type(p.get('end')) is int and isinstance(p.get('quote_excerpt'),str) and p['end']==len(p['quote_excerpt']) and p.get('excerpt_status')=='complete_capture_field' and p.get('quote_truncated') is False for p in ps) and {p.get('source_field') for p in ps}=={ex.get('command_field'),ex.get('result_field')} and ex.get('tool_name','').lower() in r._EXECUTION_TOOLS and type(ex.get('exit_code')) is int):return node
 command=next((p.get('quote_excerpt') for p in ps if p.get('source_field')==ex.get('command_field')),None)
 if command!=ex.get('command'):return node
 old=node
 node=node.replace(' field_association="original_execution_capture"',' field_association="original_execution_capture" complete_capture_fields="true"',1)
 def source_tag(m):return m.group().replace(' quote_truncated="False"','').replace(' excerpt_status="complete_capture_field"','')
 node=re.sub(r'<source\b[^>]*>',source_tag,node)
 # Own source citation attributes and atomic command/exit/tool header stay.
 for a,b in zip(ET.fromstring(old).iter('source'),ET.fromstring(node).iter('source')):
  for key in ('source_id','source_field','source_role','tool_use_id','start','end'):
   assert a.get(key)==b.get(key)
  assert a.findtext('quote_excerpt')==b.findtext('quote_excerpt')
 assert ET.tostring(ET.fromstring(old).find('execution'))==ET.tostring(ET.fromstring(node).find('execution'))
 state['native_groups_compacted']+=1;state['native_serialized_chars_saved']+=len(old)-len(node)
 return node

def source_compact(source,*,complete=False):
 shown=dict(source)
 if state['mode']=='conservative' and complete and shown.get('source_role')=='assistant_report' and shown.get('excerpt_status')=='complete_source' and shown.get('start')==0 and type(shown.get('end')) is int and isinstance(shown.get('quote_excerpt'),str) and shown['end']==len(shown['quote_excerpt']) and type(shown.get('source_chars')) is int and shown['source_chars']==shown['end'] and not shown.get('quote_truncated'):
  before=original_source(shown,complete=complete);shown.pop('source_chars');after=original_source(shown,complete=complete);state['report_pointer_chars_saved']+=len(before)-len(after);return after
 return original_source(shown,complete=complete)

def attrs_compact(record,projection):
 shown=record
 caller=sys._getframe(1)
 if state['mode']=='conservative' and caller.f_code.co_name=='_plan_original_source_transport' and record.get('lane')=='source_excerpts' and re.fullmatch(r'selected-source-\d+',str(record.get('id',''))):
  brief=caller.f_locals.get('brief',{})
  if not any(record['id'] in (rel.get('from'),rel.get('to')) for rel in brief.get('relations',[])):
   shown={k:v for k,v in record.items() if k!='id'}
   state['synthetic_item_id_chars_saved']+=len(original_attrs(record,projection))-len(original_attrs(shown,projection))
 return original_attrs(shown,projection)

def catalog(xml):
 out=[];seen=set()
 for root in re.findall(r'<project-memory\b.*?</project-memory>',xml,re.S):
  for p in ET.fromstring(root).iter('source'):
   text=p.findtext('quote_excerpt')
   if not text or not text.strip():continue
   q={'source_id':p.get('source_id'),'source_role':p.get('source_role'),'source_field':p.get('source_field','body'),'start':int(p.get('start')),'end':int(p.get('start'))+len(text),'quote':text}
   if p.get('tool_use_id'):q['tool_use_id']=p.get('tool_use_id')
   key=json.dumps(q,sort_keys=True)
   if key not in seen:seen.add(key);out.append(q)
 return out

def covered(old,new,mirror=False):
 same=[p for p in new if p['source_id']==old['source_id'] and p['source_role']==old['source_role'] and (mirror or p['source_field']==old['source_field'] and p.get('tool_use_id')==old.get('tool_use_id'))]
 intervals=[]
 for p in same:
  lo=max(old['start'],p['start']);hi=min(old['end'],p['end'])
  if lo<hi and old['quote'][lo-old['start']:hi-old['start']]==p['quote'][lo-p['start']:hi-p['start']]:intervals.append((lo,hi))
 cursor=old['start']
 for lo,hi in sorted(intervals):
  if lo>cursor:return False
  cursor=max(cursor,hi)
 return cursor>=old['end']

before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'codex_mem').glob('*.py')}
variants={};r._execution_xml=native_compact;r._source_xml=source_compact;r._item_attrs=attrs_compact
for mode in ('baseline','conservative'):
 state.update(mode=mode,native_groups_compacted=0,native_serialized_chars_saved=0,report_pointer_chars_saved=0,synthetic_item_id_chars_saved=0)
 views=[]
 for case,held in zip(D['cases'],C):
  parts=[];owners=[]
  for owner in case['owners']:
   xml=r.render_project_brief(deepcopy(owner['pre_render_brief']),budget=owner['budget'])
   assert len(xml)<=owner['budget'];ET.fromstring(xml)
   parts.append('PROJECT '+owner['project_key']+'\n'+xml);owners.append({'project_key':owner['project_key'],'chars':len(xml),'budget':owner['budget']})
  memory='\n\n'.join(parts);assert len(memory)<=6000
  if mode=='baseline':assert memory==held['new_context']['memory']
  new=catalog(memory); old_v5=catalog(held['new_context']['memory']); old_v8=held['old_catalog']
  losses_v5=[p for p in old_v5 if not covered(p,new)]; losses_v8=[p for p in old_v8 if not covered(p,new)]
  mirror_losses_v8=[p for p in old_v8 if not covered(p,new,True)]
  views.append({'case':case['case'],'owners':owners,'catalog':new,'lost_v5_spans':losses_v5,'lost_v8_spans':losses_v8,'lost_v8_same_source_exact_mirror_spans':mirror_losses_v8,'xml':memory})
 variants[mode]={'instrumentation_counts_not_unique':{k:v for k,v in state.items() if k!='mode'},'views':views,'totals':{'source_spans':sum(len(v['catalog']) for v in views),'lost_v5_spans':sum(len(v['lost_v5_spans']) for v in views),'lost_v8_spans':sum(len(v['lost_v8_spans']) for v in views),'lost_v8_same_source_exact_mirror_spans':sum(len(v['lost_v8_same_source_exact_mirror_spans']) for v in views),'context_chars':sum(sum(o['chars'] for o in v['owners']) for v in views)}}
assert before=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'codex_mem').glob('*.py')}
output={'status':'read_only_semantically_qualified_conservative_counterfactual_not_product_or_quality','product_unchanged':True,'models':0,'store_operations':0,'gold_answers_grades_read':0,'transformations':['Keep command and tool_use_id execution header literally unchanged.','Verified native pair only: replace two identical complete_capture_field statuses and False flags by one complete_capture_fields=true on the same atomic item. Every source retains its own role/field/tool_use_id/offsets/direct quote.','Omit a redundant assistant source_chars only when explicit complete_source+zero/full offsets and exact literal length prove it.','Drop only unreferenced synthetic selected-source container IDs at their actual builder. Canonical/source IDs remain.'],'limits':['This is a diagnostic runtime-only serializer transformation. No product source file changed.','Catalog is an independent direct-source XML collector with the same visible-span identity, not a modification of the frozen helper. Parent must use the frozen assessor for an implemented candidate.','Catalog old-span accounting is generic across96views and does not use gold. Actual native writer compatibility and reference metadata preservation are separate required reviews.'],'held_hashes':before,'variants':variants}
p=ROOT/'docs/evidence/project-memory-quality-2026-10-21-r17/development-v6-design/conservative-counterfactual.json';p.write_text(json.dumps(output,indent=2)+'\n')
print({k:v['totals'] for k,v in variants.items()});print('sha256',hashlib.sha256(p.read_bytes()).hexdigest())
