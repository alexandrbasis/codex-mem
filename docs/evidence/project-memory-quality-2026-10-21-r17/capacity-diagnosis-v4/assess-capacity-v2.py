"""Saved XML union costs, not renderer/retrieval/quality evaluation."""
from collections import Counter, defaultdict
import hashlib,html,json,re
from pathlib import Path
from xml.etree import ElementTree as ET
ROOT=Path(__file__).resolve().parents[4]
R17=ROOT/'docs/evidence/project-memory-quality-2026-10-21-r17'
OUT=Path(__file__).resolve().parent

def read(p):return json.loads(p.read_text())
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def attrs(values):return ''.join(' '+k+'="'+html.escape(str(v),quote=True)+'"'for k,v in values.items())
def merge(intervals):
 result=[]
 for a,b in sorted(set(intervals)):
  if result and a<=result[-1][1]:result[-1][1]=max(b,result[-1][1])
  else:result.append([a,b])
 return result

def part(memory,key):return next(xml for k,xml in re.findall(r'PROJECT ([^\n]+)\n(<project-memory\b.*?</project-memory>)',memory,re.S)if k==key)
def identity(s):return(s['source_id'],s.get('source_field','body'),s['source_role'],s.get('tool_use_id'))
def ownsource(ident,a,b,text):
 sid,field,role,tool=ident;v={'source_id':sid,'source_field':field,'source_role':role,'start':a,'end':b}
 if tool is not None:v['tool_use_id']=tool
 return '<source'+attrs(v)+'><quote_excerpt>'+html.escape(text,quote=False)+'</quote_excerpt></source>'

fixture_path=Path('/private/tmp/codex-mem-quality-20261021-r16-sealed/holdout-fixture.json')
fixture=read(fixture_path);histories={h['id']:h for h in fixture['histories']}
v4p=R17/'development-v4/central-render-assessment/contexts-and-coverage.json';v3p=R17/'development-v3/central-render-assessment/contexts-and-coverage.json'
v4=read(v4p);v3={v['case']:v for v in read(v3p)};dto_path=R17/'frozen-all96-pre-render-dtos.json';dtos={v['case']:v for v in read(dto_path)['cases']};goldlosses={(f['case'],f['fact_index'])for f in read(R17/'development-v4/central-render-assessment/result.json')['gold_visibility']['mirror_losses']};rows=[];totals=Counter()
for view in v4:
 hid,qid=view['case'].split('/');hist=histories[hid];gen=read(Path('/private/tmp/codex-mem-quality-20261021-r16-experiment/candidate')/hid/'generation.json');originals={};captures={};source_scope={}
 for session in hist['sessions']:
  for event in session['events']:
   sid=gen['source_map'].get(event['id'])
   if sid is None:continue
   fields={'body':event.get('text','')}
   cap=event.get('tool_capture')
   if cap:
    for box,names in [('tool_input',('command','cmd')),('tool_response',('output','stdout','stderr'))]:
     for name in names:
      if isinstance(cap.get(box,{}).get(name),str):fields[box+'.'+name]=cap[box][name]
    captures[sid]=cap
   originals[sid]=fields;source_scope[sid]=session.get('project_key',hist['project_keys'][0])
 for owner in view['owners']:
  key=owner['project_key'];budget=owner['budget'];newxml=part(view['new_context']['memory'],key);oldcat=[s for s in view['old_catalog']if source_scope[s['source_id']]==key];newcat=[s for s in view['new_catalog']if source_scope[s['source_id']]==key];allcat=oldcat+newcat;groups=defaultdict(list)
  for s in allcat:groups[identity(s)].append((s['start'],s['end']))
  # Atomic completion only for captures already represented as executions, identified by own tool identity.
  atomic={sid for sid,field,role,tool in groups if tool is not None and sid in captures}
  for sid in atomic:
   cap=captures[sid];toolids={tool for source,_,_,tool in groups if source==sid and tool is not None}
   for tool in toolids:
    for field in ('tool_input.command','tool_input.cmd','tool_response.output','tool_response.stdout','tool_response.stderr'):
     if field in originals[sid]:groups[(sid,field,'tool_record',tool)].append((0,len(originals[sid][field])))
  nodes=[];payload=0;table_refs={};table_index=0
  for ident,intervals in groups.items():
   text=originals[ident[0]][ident[1]]
   for a,b in merge(intervals):
    table_index+=1;ref='S'+str(table_index);table_refs[(ident,a,b)]=ref
    node=ownsource(ident,a,b,text[a:b]).replace('<source ','<source transport_ref=\"'+ref+'\" ',1)
    nodes.append(node);payload+=len(html.escape(text[a:b],quote=False))
  execution_nodes=[]
  for sid in sorted(atomic):
   cap=captures[sid]
   for tool in sorted({tool for source,_,_,tool in groups if source==sid and tool is not None}):
    execution_nodes.append('<execution'+attrs({'source_id':sid,'tool_use_id':tool,'tool_name':cap['tool_name'],'exit_code':cap['tool_response']['exit_code'],'field_association':'original_execution_capture'})+'/>')
  lowerxml='<project-memory trust="untrusted" freshness="not_verified">'+''.join(execution_nodes)+''.join(nodes)+'</project-memory>'
  # Constructive but deliberately bulky upper witness: retain every actual parent qualifier/association.
  # Exact source quotes are emitted once; subsequent original association pointers are references.
  current_old=read(Path('/private/tmp/codex-mem-quality-20261021-r16-experiment/candidate')/hid/'contexts.json');oldmemory=next(q['memory']for q in current_old if q['id']==qid);oldxml=part(oldmemory,key)
  qualifications=[];compact_qualifications=[];seen=set();compact_seen=set();extra_attrs=Counter();duplicate_quote=0
  for xml in (oldxml,newxml):
   root=ET.fromstring(xml)
   for item in root.findall('item'):
    srcs=list(item.iter('source'))
    if not srcs:continue
    # Preserve exact item identity, state, custody, role, scope, uncertainty, history and field qualification.
    refs=[]
    for field in item.findall('field'):
     pointers=[]
     for src in field.iter('source'):
      raw=dict(src.attrib);raw.pop('quote_display',None)
      if src.find('quote_excerpt')is not None:duplicate_quote+=len(html.escape(''.join(src.find('quote_excerpt').itertext()),quote=False))
      pointers.append('<source'+attrs({**raw,'quote_display':'in_source_table'})+'/>')
     if pointers:refs.append('<field'+attrs(field.attrib)+'>'+''.join(pointers)+'</field>')
    # Direct source children, including atomic captures, retain their own qualifier pointers.
    for src in item.findall('source'):
     raw=dict(src.attrib);raw.pop('quote_display',None);refs.append('<source'+attrs({**raw,'quote_display':'in_source_table'})+'/>')
    node='<item'+attrs(item.attrib)+'>'+''.join(refs)+'</item>'
    compact_fields=[]
    for field in item.findall('field')+[None]:
     pointers=list(item.findall('source'))if field is None else list(field.iter('source'));uses=[]
     for src in pointers:
      ident=(src.get('source_id'),src.get('source_field','body'),src.get('source_role'),src.get('tool_use_id'))
      if src.get('start')is None or src.get('end')is None:continue
      a,b=int(src.get('start')),int(src.get('end'));ref=next((label for (iid,lo,hi),label in table_refs.items()if iid==ident and lo<=a and hi>=b),None)
      if ref is None:continue
      qualifier={k:v for k,v in src.attrib.items()if k not in ('source_id','source_field','source_role','tool_use_id','quote_display')}
      uses.append('<use'+attrs({'source_ref':ref,**qualifier})+'/>')
     if uses:compact_fields.append(''.join(uses)if field is None else '<field'+attrs(field.attrib)+'>'+''.join(uses)+'</field>')
    compact_node='<item'+attrs(item.attrib)+'>'+''.join(compact_fields)+'</item>'
    if compact_node not in compact_seen:compact_seen.add(compact_node);compact_qualifications.append(compact_node)
    if node not in seen:seen.add(node);qualifications.append(node)
    for n in ('quote_truncated','source_chars','excerpt_status','source_role','source_capture_seq','capture_order_basis'):
     extra_attrs[n]+=sum(len(m.group())for m in re.finditer(r' '+n+r'="[^"]*"',ET.tostring(item,encoding='unicode')))
  qualifierxml=''.join(qualifications)
  rootattrs=dict(ET.fromstring(oldxml).attrib);rootattrs.update(ET.fromstring(newxml).attrib)
  compactxml='<project-memory'+attrs(rootattrs)+'>'+''.join(execution_nodes)+''.join(nodes)+''.join(compact_qualifications)+'<omitted derived_fields=\"true\"/></project-memory>'
  upperxml='<project-memory'+attrs(rootattrs)+'>'+''.join(execution_nodes)+''.join(nodes)+qualifierxml+'<omitted derived_fields="true"/></project-memory>'
  lost=[s for s in view['losses_same_field_role_tool_identity']if source_scope[s['source_id']]==key]
  oldspanloss=[s for s in view['old_spans_not_fully_covered']if source_scope[s['source_id']]==key]
  label='proven_infeasible_even_without_qualifiers'if len(lowerxml)>budget else'constructive_qualified_union_fits'if len(compactxml)<=budget else'lower_bound_fits_full_qualification_fit_unproven'
  totals.update(parts=1,**{label:1});
  if lost:totals.update(affected_parts=1,**{'affected_'+label:1})
  rows.append({'case':view['case'],'project_key':key,'owner_budget':budget,'old_v8_xml_chars':len(oldxml),'v3_xml_chars':len(part(v3[view['case']]['new_context']['memory'],key)),'v4_xml_chars':len(newxml),'old_span_losses':oldspanloss,'exact_lost_intervals':lost,'union_unique_interval_count':sum(len(merge(v))for v in groups.values()),'union_escaped_quote_payload_chars':payload,'atomic_capture_count':len(atomic),'canonical_union_lower_bound_chars':len(lowerxml),'qualification_preserving_upper_witness_chars':len(upperxml),'exact_source_reference_compact_witness_chars':len(compactxml),'compact_reference_witness_deficit':max(0,len(compactxml)-budget),'qualification_reference_chars':len(qualifierxml),'fit_classification':label,'lower_bound_deficit':max(0,len(lowerxml)-budget),'upper_witness_deficit':max(0,len(upperxml)-budget),'representative_atomic_sizes':[{'source_id':sid,'complete_field_quote_transport_chars':sum(len(node)for node in nodes if sid in node),'execution_tag_chars':sum(len(node)for node in execution_nodes if sid in node)}for sid in atomic],'saved_repeated_attribute_chars':dict(extra_attrs),'lower_bound_caution':'Drops scope/custody/history/association metadata deliberately; if this exceeds budget, fit is impossible. A fitting lower bound alone proves nothing.','compact_witness_semantics':'Each use source_ref resolves to one exact source/field/role/tool identity with exact start/end still on use; all actual item/field and remaining pointer qualifiers remain. Own canonical source attrs+directquotes stay in source table. This future reference schema requires explicit reader contract documentation; no body/output substitution.',
'upper_witness_caution':'Keeps every original item/field/source qualifier as an explicit reference, with unique canonical quote table. This may overpay redundant association references; exceeding budget does not prove infeasibility. Uses a future explicit quote-reference schema, not current helper catalog validation.'})
result={'status':'independent_saved_capacity_bounds_not_quality','inputs':{str(p):digest(p)for p in (v4p,v3p,dto_path,fixture_path)},'counts':dict(totals),'owner_parts':rows,'v4_gold_mirror_loss_keys':[{'case':c,'fact_index':i}for c,i in sorted(goldlosses)],'recommendation':{'algorithm':'One exact source/field/role/tool-identity interval inventory, union overlapping literal intervals only, atomic capture completion as indivisible groups, then one deterministic whole-group packing pass. Price source table and qualifier references together; deduplicate exact qualifier records only. Require an old-coverage feasibility check before admitting additions; no body/output equivalence or cross-source substitute.','qualifier_rule':'Source-own canonical IDs/fields/roles/offsets/directquotes stay on quoted spans; custody/state/history/environment/task identity qualifiers remain explicit per association or exact compatible shared container. Do not erase independent scope/uncertainty to make a number fit.','decision_rule':'If mandatory union lower bound exceeds owner budget, report explicitly infeasible and choose/document a bounded coverage tradeoff. If upper witness fits, a constructively feasible serializer exists. Between bounds, optimize exact duplicate metadata and measure actual serializer; do not assert fit from payload alone.','admission':'No new source acquisition or query relaxation. V4 already-admitted gained sources compete with old source groups under unchanged owner budgets.','not_an_implementation':'Bounds/prospective algorithm only; no product code executed or changed.'},'models':0,'store_sql_retrieval_operations':0,'answers_grades_read':0};p=OUT/'capacity-bounds-v2-reference-witness.json';assert not p.exists();p.write_text(json.dumps(result,indent=2)+'\n');print(dict(totals));print('sha',digest(p))
if __name__=='__main__':pass
