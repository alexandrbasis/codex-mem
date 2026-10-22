"""Pure frozen-renderer tracing over exact replayed DTOs; no databases or retrieval."""
import hashlib,inspect,json,linecache,pathlib,sys
R=pathlib.Path(__file__).resolve().parents[3];B=R/'docs/evidence/project-memory-quality-2026-10-21-r17'
x=json.loads((B/'frozen-all96-pre-render-dtos.json').read_text());sys.path.insert(0,x['runtime'])
import codex_mem.project_memory_retrieval as ret
assert pathlib.Path(ret.__file__).resolve().is_relative_to(pathlib.Path(x['runtime']))
pairs=json.loads((R/'docs/evidence/project-memory-quality-2026-10-21-r16/source-visibility-final.json').read_text())['paired_exact_mirror_losses'];wanted={(p['candidate']['case'],s['source_id'])for p in pairs for s in p['candidate']['support']};out=[];events=[];ids=[]
def matches(value):
 text=json.dumps(value,default=str);return [sid for sid in ids if sid in text]
def trace(frame,event,arg):
 if event!='line' or frame.f_code.co_filename!=ret.__file__:return trace
 name=frame.f_code.co_name;loc=frame.f_locals;line=linecache.getline(frame.f_code.co_filename,frame.f_lineno).strip()
 if name not in ('_plan_original_source_transport','_select_bundle_fields','_render_project_brief'):return trace
 focus=loc.get('group')if name=='_plan_original_source_transport'else loc.get('field')if name=='_select_bundle_fields'else loc.get('record')
 hits=matches(focus) if focus else []
 if not hits:return trace
 important=(name=='_plan_original_source_transport'and ('if cost +'in line or 'if is_neutral and neutral_selected' in line))or(name=='_select_bundle_fields'and 'if size <= maximum' in line)or(name=='_render_project_brief'and('if not node or len(node)'in line or 'if len(node)'in line or 'if record.get' in line or 'if sum(map(len, parts))'in line or line=='continue'))
 if important:
  vals={k:loc[k]for k in ('available','cost','maximum','size','neutral_limit','neutral_spent','neutral_selected','remaining','item_budget','compact_reserve','execution_reserve','head_reserve','index','omitted')if k in loc and isinstance(loc[k],(int,float,bool,str))};vals.update(node_chars=len(loc['node'])if isinstance(loc.get('node'),str)else None,marker_chars=len(loc['marker'])if isinstance(loc.get('marker'),str)else None,record_id=(loc.get('record')or{}).get('id'))
  events.append({'function':name,'line':frame.f_lineno,'code':line,'source_ids':hits,'values':vals})
 return trace
for case in x['cases']:
 for owner in case['owners']:
  ids=[sid for c,sid in wanted if c==case['case']];ids=[sid for sid in ids if any(s['support']['source_id']==sid for s in owner['sources'])]
  if not ids:continue
  events=[];brief=owner['pre_render_brief'];sys.settrace(trace)
  try:xml=ret.render_project_brief(brief,budget=owner['budget'])
  finally:sys.settrace(None)
  actual='PROJECT '+owner['project_key']+'\n'+xml
  # Whole-context fidelity was verified in the database-copy capture; repeat exact owner rendering here.
  assert len(xml)==owner['xml_chars']
  originals=[]
  for record in brief['records']:
   hit=matches(record)
   if hit:
    atomic=ret._execution_xml(record,brief.get('projection'))
    originals.append({'id':record['id'],'lane':record['lane'],'source_ids':hit,'atomic_execution_chars':len(atomic),'atomic_xml':atomic or None,'record':record})
  out.append({'case':case['case'],'project_key':owner['project_key'],'budget':owner['budget'],'rendered_chars':len(xml),'free_chars':owner['budget']-len(xml),'fit_events':events,'target_records':originals,'owner_render_sha256':hashlib.sha256(xml.encode()).hexdigest()})
p=B/'frozen-paired-loss-fit-events.json';assert not p.exists();p.write_text(json.dumps({'status':'frozen_pure_dto_serializer_fit_trace','dto_sha256':hashlib.sha256((B/'frozen-all96-pre-render-dtos.json').read_bytes()).hexdigest(),'cases':out,'models':0,'store_sql_retrieval_operations':0},indent=2)+'\n');print(hashlib.sha256(p.read_bytes()).hexdigest())
for c in out:
 print(c['case'],[(e['function'],e['values'])for e in c['fit_events']if e['function']!='_render_project_brief'][:7])
