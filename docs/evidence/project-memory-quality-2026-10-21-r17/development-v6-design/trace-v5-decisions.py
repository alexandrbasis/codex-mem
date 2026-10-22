import sys,json,inspect,hashlib
from pathlib import Path
from copy import deepcopy
from xml.etree import ElementTree as ET
ROOT=Path('/Users/alexandrbasis/Desktop/Coding/codex-mem-public')
sys.path.insert(0,str(ROOT))
from codex_mem import project_memory_retrieval as r
from codex_mem.project_memory_source_transport import OriginalSourcePlan
D=json.loads((ROOT/'docs/evidence/project-memory-quality-2026-10-21-r17/frozen-all96-pre-render-dtos.json').read_text())
C=json.loads((ROOT/'docs/evidence/project-memory-quality-2026-10-21-r17/development-v5/central-render-assessment/contexts-and-coverage.json').read_text())
source=Path(r.__file__).read_text().splitlines()
lines={}
for i,line in enumerate(source,1):
 for key,needle in [('group_fit','if cost + (len(marker)'),('group_accept','available -= cost'),('field_fit','if len(whole) <= remaining_item:'),('field_accept','content += whole'),('item_fit','if len(item_xml) <= min(remaining, item_budget):')]:
  if needle in line:lines[key]=i
stage_boundary=next(i for i,line in enumerate(source,1) if 'source_nodes, source_pointers, neutral_selected' in line)
state={};allviews=[]
def spans(refs):
 return [{k:p.get(k) for k in ('source_id','source_field','source_role','tool_use_id','start','end')} | {'quote_chars':len(p.get('quote_excerpt') or '')} for p in refs]
def trace(frame,event,arg):
 if event!='line' or frame.f_code.co_filename != r.__file__:return trace
 loc=frame.f_locals;lineno=frame.f_lineno
 if lineno==lines['group_fit']:
  g=loc['group']; row={'stage':'complementary' if loc.get('complementary') else 'user_original','refs':spans(g['refs']),'neutral':bool(g.get('neutral')),'custody':bool(g.get('custody')),'qualifiers':g['qualifiers'],'cost':loc['cost'],'available':loc['available'],'neutral_spent':loc['neutral_spent'],'neutral_limit':loc['neutral_limit'],'marker_chars':len(loc['marker']) if loc['neutral'] else 0,'accepted':False,'container_refs_before':len(loc['container'].get('refs',[])),'new_group_refs':len(g['refs'])}
  state['groups'].append(row)
 elif lineno==lines['group_accept']:
  state['groups'][-1]['accepted']=True
 elif lineno==lines['field_fit']:
  state['fields'].append({'record_id':loc['record'].get('id'),'lane':loc['record'].get('lane'),'field':loc['field'].get('name'),'refs':spans(loc['refs']),'node_chars':len(loc['whole']),'remaining_item':loc['remaining_item'],'item_budget':loc['item_budget'],'remaining_owner':loc['remaining'],'head_reserve':loc['head_reserve'],'compact_reserve':loc['compact_reserve'],'accepted':False})
 elif lineno==lines['field_accept']:
  state['fields'][-1]['accepted']=True
 elif lineno==lines['item_fit']:
  state['items'].append({'record_id':loc['record'].get('id'),'lane':loc['record'].get('lane'),'node_chars':len(loc['item_xml']),'remaining_owner':loc['remaining'],'item_budget':loc['item_budget'],'fits':len(loc['item_xml'])<=min(loc['remaining'],loc['item_budget'])})
 return trace
original_offer=OriginalSourcePlan.offer
def offer(self,node):
 before=self.remaining;caller=sys._getframe(1);outer=caller.f_back
 label=('protected_role' if outer.f_lineno<stage_boundary else 'additional_role') if caller.f_code.co_name=='offer_role' else 'source_container_commit'
 result=original_offer(self,node)
 root=ET.fromstring(node) if node else None
 state['offers'].append({'stage':label,'caller_line':outer.f_lineno,'node_chars':len(node),'remaining_before':before,'accepted':result,'node_kind':root.tag if root is not None else None,'id':root.get('id') if root is not None else None,'lane':root.get('lane') if root is not None else None,'refs':spans([p.attrib for p in root.iter('source')]) if root is not None else []})
 return result
OriginalSourcePlan.offer=offer
before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'codex_mem').glob('*.py')}
for case,old in zip(D['cases'],C):
 assert case['case']==old['case']
 owners=[];parts=[]
 for owner in case['owners']:
  state={'groups':[],'fields':[],'items':[],'offers':[]}
  sys.settrace(trace)
  try:xml=r.render_project_brief(deepcopy(owner['pre_render_brief']),budget=owner['budget'])
  finally:sys.settrace(None)
  owners.append({'project_key':owner['project_key'],'budget':owner['budget'],'xml_chars':len(xml),'decisions':state})
  parts.append('PROJECT '+owner['project_key']+'\n'+xml)
 assert '\n\n'.join(parts)==old['new_context']['memory'],case['case']
 allviews.append({'case':case['case'],'owners':owners,'losses':old['losses_same_field_role_tool_identity']})
assert before=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'codex_mem').glob('*.py')}
output={'status':'pure_v5_decision_trace_all96_byte_exact_no_product_edits','models':0,'store_operations':0,'gold_answers_grades_read':0,'held_source_hashes':before,'line_keys':lines,'views':allviews}
path=ROOT/'docs/evidence/project-memory-quality-2026-10-21-r17/development-v6-design/v5-decision-trace.json'
path.write_text(json.dumps(output,indent=2)+'\n')
print({'views':len(allviews),'owner_parts':sum(len(v['owners']) for v in allviews),'trace_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
