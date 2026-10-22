import sys,json,re,hashlib
from pathlib import Path
from copy import deepcopy
from xml.etree import ElementTree as ET
ROOT=Path('/Users/alexandrbasis/Desktop/Coding/codex-mem-public');sys.path.insert(0,str(ROOT))
from codex_mem import project_memory_retrieval as r
D=json.loads((ROOT/'docs/evidence/project-memory-quality-2026-10-21-r17/frozen-all96-pre-render-dtos.json').read_text())
DECL=' quote_ref_semantics="quotation_only_no_qualifier_inheritance"'

def catalog(xml):
 out=[];seen=set()
 for p in ET.fromstring(xml).iter('source'):
  text=p.findtext('quote_excerpt')
  if not text or not text.strip():continue
  row={'source_id':p.get('source_id'),'source_role':p.get('source_role'),'source_field':p.get('source_field','body'),'start':int(p.get('start')),'end':int(p.get('start'))+len(text),'quote':text}
  if p.get('tool_use_id'):row['tool_use_id']=p.get('tool_use_id')
  key=json.dumps(row,sort_keys=True)
  if key not in seen:seen.add(key);out.append(row)
 return out

def covered(span,cat):
 intervals=[]
 for q in cat:
  if any(span.get(k)!=q.get(k) for k in ('source_id','source_role','source_field','tool_use_id')):continue
  lo=max(span['start'],q['start']);hi=min(span['end'],q['end'])
  if lo<hi and span['quote'][lo-span['start']:hi-span['start']]==q['quote'][lo-q['start']:hi-q['start']]:intervals.append((lo,hi))
 end=span['start']
 for lo,hi in sorted(intervals):
  if lo>end:return False
  end=max(end,hi)
 return end>=span['end']

def compact_node(m):
 node=m.group();root=ET.fromstring(node);ex=root.find('execution');ps=list(root.findall('source'))
 valid=ex is not None and root.get('field_association')=='original_execution_capture' and len(ps)==2 and ex.get('tool_name','').lower() in r._EXECUTION_TOOLS and bool(ex.get('tool_use_id'))
 if valid:
  valid=all(p.get('source_role')=='tool_record' and p.get('source_id')==root.get('id') and p.get('tool_use_id')==ex.get('tool_use_id') and p.get('start')=='0' and p.findtext('quote_excerpt') is not None and int(p.get('end'))==len(p.findtext('quote_excerpt')) and p.get('quote_truncated')=='False' and p.get('excerpt_status')=='complete_capture_field' for p in ps)
  commands=[p for p in ps if p.get('source_field') in {'tool_input.cmd','tool_input.command'}]
  outputs=[p for p in ps if p.get('source_field') in {'tool_response.output','tool_response.stdout','tool_response.stderr'}]
  valid=valid and len(commands)==len(outputs)==1 and commands[0].findtext('quote_excerpt')==ex.get('command')
 if valid:
  old=node;node=node.replace(' field_association="original_execution_capture"',' field_association="original_execution_capture" complete_capture_fields="true"',1)
  node=re.sub(r'<source\b[^>]*>',lambda s:s.group().replace(' quote_truncated="False"','').replace(' excerpt_status="complete_capture_field"',''),node)
  assert ET.tostring(ET.fromstring(old).find('execution'))==ET.tostring(ET.fromstring(node).find('execution'))
 if root.get('lane')=='source_excerpts' and re.fullmatch(r'selected-source-\d+',root.get('id','')) and root.get('id') not in incoming:
  node=node.replace(' id='+r._attr(root.get('id')),'',1)
 # A report's complete-source flag and its exact full interval retain the proof.
 for p in root.iter('source'):
  text=p.findtext('quote_excerpt')
  if p.get('source_role')=='assistant_report' and p.get('excerpt_status')=='complete_source' and p.get('start')=='0' and text is not None and int(p.get('end'))==len(text) and p.get('source_chars')==str(len(text)) and p.get('quote_truncated') in {None,'False'}:
   token=' source_chars='+r._attr(p.get('source_chars'));oldopening=next((x.group() for x in re.finditer(r'<source\b[^>]*>',node) if 'source_id='+r._attr(p.get('source_id')) in x.group()),None)
   if oldopening and token in oldopening:node=node.replace(oldopening,oldopening.replace(token,''),1)
 return node

before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'codex_mem').glob('*.py')}
views=[];summary={'owners':0,'v6_catalog_entries':0,'lost_v6_catalog_entries':0,'appended_body_groups':0,'declaration_unfit_without_eviction':0,'canonical_chars':0,'compacted_chars':0,'final_chars':0}
for case in D['cases']:
 owners=[]
 for owner in case['owners']:
  brief=deepcopy(owner['pre_render_brief']);canonical=r.render_project_brief(brief,budget=owner['budget']);root=ET.fromstring(canonical)
  incoming={v for relation in brief.get('relations',[]) for v in (relation.get('from'),relation.get('to')) if v}
  compacted=re.sub(r'<item\b[^>]*>.*?</item>|<item\b[^>]*/>',compact_node,canonical,flags=re.S)
  basecatalog=catalog(canonical);assert all(covered(span,catalog(compacted)) for span in basecatalog)
  declaration_cost=len(DECL) if root.find('.//quote_ref') is not None else 0
  declaration_fit=len(compacted)+declaration_cost<=owner['budget']
  wire=compacted
  if declaration_fit and declaration_cost:wire=wire.replace('<project-memory','<project-memory'+DECL,1)
  summary['declaration_unfit_without_eviction']+=not declaration_fit
  current_catalog=catalog(wire);used={p.get('id') for p in ET.fromstring(wire).iter('source') if p.get('id')};nextid=1
  def freshid():
   global nextid
   while 's'+str(nextid) in used:nextid+=1
   value='s'+str(nextid);used.add(value);nextid+=1;return value
  candidates=[]
  # Read-only feasibility intentionally refills only the pre-existing bounded
  # complementary body stream. It never reprices or evicts the selected prefix.
  for group in r._complete_source_groups(brief['records']):
   refs=group['refs'];spans=[{'source_id':p['source_id'],'source_field':p.get('source_field') or 'body','source_role':p.get('source_role'),'start':p['start'],'end':p['end'],'quote':p['quote_excerpt'],**({'tool_use_id':p['tool_use_id']} if p.get('tool_use_id') else {})} for p in refs]
   if all(covered(p,current_catalog) for p in spans):continue
   qualifiers=deepcopy(group['qualifiers'])
   if root.get('worktree') and (qualifiers.get('scope') or {}).get('worktree')==root.get('worktree'):
    qualifiers['scope']={k:v for k,v in qualifiers['scope'].items() if k!='worktree'}
   node='<item'+r._item_attrs({'lane':'source_excerpts',**qualifiers},brief.get('projection'))+'><field basis="attributed_source_excerpts" field_association="not_assessed">'+''.join(r._source_xml({**p,'id':freshid()},complete=True) for p in refs)+'</field></item>'
   fits=declaration_fit and len(wire)+len(node)<=owner['budget']
   candidates.append({'source_ids':[p['source_id'] for p in refs],'spans':spans,'node_chars':len(node),'free_before':owner['budget']-len(wire),'fits':fits})
   if fits:
    wire=wire.replace('</project-memory>',node+'</project-memory>');current_catalog=catalog(wire);summary['appended_body_groups']+=1
  losses=[p for p in basecatalog if not covered(p,current_catalog)];assert not losses
  summary['owners']+=1;summary['v6_catalog_entries']+=len(basecatalog);summary['lost_v6_catalog_entries']+=len(losses);summary['canonical_chars']+=len(canonical);summary['compacted_chars']+=len(compacted);summary['final_chars']+=len(wire)
  assert len(wire)<=owner['budget']
  owners.append({'project_key':owner['project_key'],'budget':owner['budget'],'canonical_chars':len(canonical),'compacted_chars':len(compacted),'saved_chars':len(canonical)-len(compacted),'declaration_chars':declaration_cost,'declaration_fit':declaration_fit,'final_chars':len(wire),'body_candidates':candidates,'base_catalog':basecatalog,'final_catalog':current_catalog,'wire':wire})
 views.append({'case':case['case'],'owners':owners})
assert before=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'codex_mem').glob('*.py')}
out={'status':'read_only_postselection_compaction_bounded_body_refill_feasibility_not_product','models':0,'store_operations':0,'gold_answers_grades_read':0,'held_hashes':before,'summary':summary,'limits':['Canonical V6 selection, all whole selected groups, associations and their metadata stay fixed. Compaction only removes proven redundant wire metadata, preserving every original direct citable source span.','The declaration must fit without eviction or refill is refused. Any declaration-unfit owner is a blocking design limit, not an accepted output.','Refill examines only the existing complementary admitted group stream in its existing order, with existing600character group limit. No new reads, native/report re-admission, user/neutral cap expansion, source alias, or partial execution.','No canonical record or field association is newly activated by an appended original body quotation. Existing omitted field/record association counts remain truthful, because associations remain unchanged.','This diagnostic does not implement the frozen helper verifier. A product candidate needs independent full catalog verification and semantic review.'], 'views':views}
p=ROOT/'docs/evidence/project-memory-quality-2026-10-21-r17/development-v6-design/monotonic-body-refill-feasibility.json';p.write_text(json.dumps(out,indent=2)+'\n');print(summary);print('sha256',hashlib.sha256(p.read_bytes()).hexdigest())
