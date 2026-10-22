"""Read-only replay. No Store constructor, models, grade writes or source changes."""
import importlib.util
import collections, dataclasses, hashlib, json, pathlib, re, sqlite3, threading, xml.etree.ElementTree as ET
from codex_mem.store import Store
from codex_mem.project_memory_query import plan_query
from codex_mem.project_memory import ProjectMemory, _fts_query, _EXECUTION_TOOLS
from codex_mem.tool_io import get_tool_capture_for_entry
import codex_mem.project_memory_retrieval as retrieval
from codex_mem.project_memory_retrieval import build_project_brief
ROOT=pathlib.Path('/private/tmp/codex-mem-quality-20261021-r10-experiment')
SEALED=pathlib.Path('/private/tmp/codex-mem-quality-20261021-r10-sealed')
def load(p): return json.loads(p.read_text())
def refs(xml):
 out=[]
 for block in re.findall(r'<project-memory\b.*?</project-memory>',xml,re.S):
  root=ET.fromstring(block)
  for item in root.findall('item'):
   for field in item.findall('field'):
    for s in field.findall('source'):
     quote=s.findtext('quote_excerpt') or ''; start=int(s.attrib['start'])
     out.append(dict(source_id=s.attrib['source_id'],source_field=s.get('source_field','body'),start=start,end=start+len(quote),quote=quote,source_role=s.get('source_role'),item_id=item.get('id'),field=field.get('name')))
 return out
def covered(start,end,rs):
 cursor=start
 for r in sorted(rs,key=lambda r:r['start']):
  if r['start']>cursor: break
  cursor=max(cursor,r['end'])
 return cursor>=end

def readonly(p):
 s=Store.__new__(Store);s.data_dir=p/'data';s.db_path=s.data_dir/'memory.sqlite3';s._closed=False;s._lock=threading.RLock()
 s._conn=sqlite3.connect(f'file:{s.db_path}?mode=ro',uri=True);s._conn.row_factory=sqlite3.Row
 s._conn.execute('PRAGMA query_only=ON');return s
spec=importlib.util.spec_from_file_location('frozen_r10_helper',pathlib.Path(__file__).with_name('frozen-helper.py'));helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
fixture=load(SEALED/'holdout-fixture.json'); gold=load(SEALED/'holdout-gold.json')['questions']; rows=[];counts=collections.Counter();losses=[]
for h in fixture['histories']:
 hid=h['id'];p=ROOT/'candidate'/hid;gen=load(p/'generation.json');contexts={x['id']:x for x in load(p/'contexts.json')};bg={x['id']:x for x in load(ROOT/'baseline'/hid/'judge-0.json')['grades']};bc={x['id']:x for x in load(ROOT/'baseline'/hid/'contexts.json')};bgen=load(ROOT/'baseline'/hid/'generation.json');questions={x['id']:x for x in h['questions']};s=readonly(p)
 for grade in load(p/'judge-0.json')['grades']:
  qid=grade['id']; lost=[i for i,f in enumerate(grade['fact_recall']) if not f['recalled'] and bg[qid]['fact_recall'][i]['recalled']]
  if grade['full_supported_answer'] and not lost:continue
  receipt=load(p/f'reader-0.q-{qid}.json');catalog=helper.reader_citation_catalog([contexts[qid]],helper.original_source_fields(h,gen['source_map']))
  assert helper.digest(catalog)==receipt['catalog_sha256']
  assert helper.digest(contexts[qid])==receipt['context_sha256']
  q=questions[qid];keys=q.get('project_keys',[q['project_key']] if 'project_key'in q else list(gen['projects']));scopes=[gen['projects'][k] for k in keys];visible=refs(contexts[qid]['memory']); probes=[];recordrefs=[];large=[]
  for k in keys:
   project=gen['projects'][k];plan=plan_query(q['question'],project,selected_project_scopes=scopes)
   selection_checks=[]
   original_source_evidence=retrieval._source_evidence
   def traced(store,owner,qp,exclude,eligible,relevant,env,version):
    for row in store._conn.execute('select * from entries where project=?',(owner,)):
     val=dict(row);val.update(text=val['body'],source_role='user_intent' if val['source'].startswith('hook:UserPromptSubmit') else 'assistant_report' if val['source'].startswith('hook:Stop') else 'tool_record')
     fts=[_fts_query(candidate,owner,topic_query=False) for candidate in qp.candidate_queries]
     hits=[bool(store._conn.execute('select 1 from entries_fts where rowid=(select rowid from entries where id=?) and entries_fts match ?',(val['id'],expression)).fetchone()) if expression else True for expression in fts]
     selection_checks.append(dict(source_id=val['id'],eligible=eligible(val),relevant=relevant(val),hard_matches=qp.matches(val),raw_fts=fts,raw_fts_hits=hits))
    return original_source_evidence(store,owner,qp,exclude,eligible,relevant,env,version)
   retrieval._source_evidence=traced
   b=build_project_brief(s,project,query=q['question'],budget=max(1000,6000//len(keys)-100),include_shadow=True,selected_project_scopes=scopes)
   retrieval._source_evidence=original_source_evidence
   snapshots=[ProjectMemory(s).snapshot(project,query=candidate,topic_query=plan.topic_query,include_episode_history=plan.assesses_current_evidence or plan.projection in {'continuity','history'}) for candidate in plan.candidate_queries]
   snapshot_custody=[item['source_id'] for snap in snapshots for item in snap.get('intent_custody',[])]
   big=build_project_brief(s,project,query=q['question'],budget=60000,include_shadow=True,selected_project_scopes=scopes)
   assert b['xml'] in contexts[qid]['memory'],(hid,qid,k,'replay mismatch')
   probes.append(dict(project_key=k,selection_checks=selection_checks,snapshot_custody=snapshot_custody,plan=dataclasses.asdict(plan),skip_reason=b.get('skip_reason'),render_omissions=b.get('render_omissions'),coverage=b['coverage'],source_coverage=b.get('source_evidence_coverage')))
   for record in b['records']:
    for f in record.get('fields',[]):
     for ref in f.get('evidence',[]): recordrefs.append(dict(ref,lane=record['lane'],field=f['name']))
   large+=refs(big['xml'])
  facts=[]
  for i,f in enumerate(gold[hid+'/'+qid]['required_facts']):
   supports=[]
   for support in f['support']:
    sid=gen['source_map'][support['event_id']];vs=[r for r in visible if r['source_id']==sid];rs=[r for r in recordrefs if r.get('source_id')==sid];ls=[r for r in large if r['source_id']==sid]
    capture=s._conn.execute('select id,project,body,source,session_id,superseded_by from entries where id=?',(sid,)).fetchone();tool=get_tool_capture_for_entry(s._conn,sid,capture['project']) if capture and capture['source'].startswith('hook:PostToolUse') else None
    objects=[dict(r) for r in s._conn.execute('select o.id,o.object_type,o.data_json from pm_objects o join pm_evidence e on e.object_id=o.id where e.source_id=?',(sid,))]
    full=covered(support['start'],support['end'],vs)
    cause='visible_full_reader_or_grade' if full else 'render_budget_or_field_selection' if covered(support['start'],support['end'],ls) else 'eligible_record_partial_field' if rs else 'query_or_source_selection' if capture else 'capture_absent'
    supports.append(dict(event_id=support['event_id'],source_id=sid,gold_span=[support['start'],support['end']],gold_quote=support['quote'],captured=dict(capture) if capture else None,tool_capture=tool,raw_tool_name_allowed=tool['tool_name'].lower() in _EXECUTION_TOOLS if tool else None,material_objects=objects,visible=vs,selected_record_refs=rs,large_budget_visible=ls,full_visible=full,cause=cause))
    if not grade['fact_recall'][i]['recalled']:counts[cause]+=1
   fact=dict(index=i,text=f['text'],recalled=grade['fact_recall'][i]['recalled'],supports=supports)
   if i in lost:
    bsid=bgen['source_map'][f['support'][0]['event_id']];fact['paired_baseline_visible']=[r for r in refs(bc[qid]['memory']) if r['source_id']==bsid];losses.append(hid+'/'+qid+':'+str(i))
   facts.append(fact)
  rows.append(dict(history=hid,question_id=qid,question=q['question'],question_context_path=str(p/'contexts.json'),question_canonical_catalog=catalog,catalog_sha256=receipt['catalog_sha256'],context_sha256=hashlib.sha256(contexts[qid]['memory'].encode()).hexdigest(),isolated_receipt_path=str(p/f'reader-0.q-{qid}.json'),canonical_catalog_path=str(p/'citation-catalog.json'),grade=grade,answer=load(p/f'reader-0.q-{qid}.json')['output'],paired_lost_fact_indices=lost,probes=probes,facts=facts))
 s._conn.close();s._closed=True
out=dict(method='Frozen source, read-only SQLite URI/query_only; no Store init; exact builder replay asserts original XML for each owner; 60000 budget diagnostic changes no query or stored state; no grading/model calls.',counts=dict(counts),paired_losses=losses,cases=rows)
print(json.dumps(out,indent=2,ensure_ascii=False))
