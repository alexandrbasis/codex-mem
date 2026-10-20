import hashlib, json, sqlite3, tempfile
from collections import Counter
from pathlib import Path
from codex_mem.store import Store
from codex_mem.project_memory import ProjectMemory, _source_task_identity
root=Path('/tmp/codex-mem-consolidation-repair-experiment')
fixture=Path('tests/fixtures/consolidation_repair_dev.json')
reasons={
'fiction-01':['Initial head has confirmed ms-81 identity.','Positive reopening has a same-description suffix outside the bounded command grammar; it is not accepted as affirmative task identity.','Late correction has no task ID in its own goal/summary source evidence.'],
'fiction-02':['Initial head has confirmed gp-24 identity.','Later cancellation names GP-24 after a for-phrase, but lacks an accepted affirmative identity marker. Cancellation is not treated as generic identity authorization.'],
'fiction-03':['Initial head has confirmed st-17 identity; the separate ST-29 sentence is excluded by the independence guard.','Closed-task and quoted/question/negative reopening evidence does not establish affirmative identity for the second head.','Later fix-under-ST-29 wording does not satisfy the bounded identity grammar; no second confirmed ST-29 head exists.'],
'fiction-04':['Font project initial head has confirmed cf-42 identity; keep-ID-open wording in the later head has no accepted identity marker.','Folder project has one confirmed cd-16 head, isolated by project.','Deleted scratch-alias source is absent; deletion is not the reason the surviving font heads fail to form a recognized pair.']}
audit={'schema_version':1,'scope':'development-only offline opportunity diagnostic','holdout_access':False,'model_calls':0,'product_or_helper_changes':False,'method':'Read saved baseline/candidate receipts and original databases in SQLite read-only mode. Call public ProjectMemory.snapshot only on disposable SQLite backup copies. Explain discovery with the same source identity predicate used by current code. No claim/commit/process calls.','fixture_path':str(fixture),'fixture_sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),'histories':[],'summary':{},'code_anchors':{'discovery':'codex_mem/project_memory.py:362','source_identity':'codex_mem/project_memory.py:1260','affirmative_reopening':'codex_mem/project_memory.py:1277'}}
for h in ['fiction-01','fiction-02','fiction-03','fiction-04']:
 gen_path=root/'baseline'/h/'generation.json';con_path=root/'candidate'/h/'consolidation.json'
 gen=json.loads(gen_path.read_text()); con=json.loads(con_path.read_text())
 record={'history_id':h,'receipt_paths':[str(gen_path),str(con_path)],'receipt_sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [gen_path,con_path]},'processing_statuses':[x['status'] for x in con['jobs']],'accepted_merge_count':con['accepted_merge_count'],'before_after_heads_equal':con['before']['heads']==con['after']['heads'],'before_after_event_coverage_equal':con['before']['event_coverage']==con['after']['event_coverage'],'reason':reasons[h],'projects':[]}
 for arm in ['baseline','candidate']:
  src=sqlite3.connect('file:'+str(root/arm/h/'data/memory.sqlite3')+'?mode=ro',uri=True)
  if arm=='baseline':
   sources=dict(src.execute('SELECT id,body FROM entries'))
   inverse={v:k for k,v in gen['source_map'].items()}
   record['heads']=[]
   for wrapped in gen['persisted_state']['episodes']:
    e=wrapped['stored_record']; refs=[r for field in ['goal','summary'] for r in e.get('evidence',{}).get(field,[]) if r.get('source_role')=='user_intent']
    record['heads'].append({'id':e['id'],'session_id':e.get('session_id'),'episode_version':e.get('episode_version'),'parent_episode_id':e.get('parent_episode_id'),'continuity_basis':e.get('continuity_basis'),'recognized_identity':_source_task_identity(e,sources),'user_identity_evidence_source_ids':sorted({r['source_id'] for r in refs}),'fixture_event_ids':sorted({inverse.get(r['source_id']) for r in refs if inverse.get(r['source_id'])})})
   record['deleted_fixture_source_ids']=[{'fixture_event_id':k,'source_id':v} for k,v in gen['source_map'].items() if v not in sources]
  with tempfile.TemporaryDirectory(prefix='cm-dev-opportunity-') as tmp:
   copy=sqlite3.connect(str(Path(tmp)/'memory.sqlite3'));src.backup(copy);copy.close()
   with Store(tmp) as store:
    for key,project in gen['projects'].items():
     snap=ProjectMemory(store).snapshot(project)
     record['projects'].append({'arm':arm,'project_key':key,'current_head_count':len(snap['episodes']),'coverage':snap['coverage']})
  src.close()
 audit['histories'].append(record)
audit['summary']={'histories':4,'project_runs':5,'baseline_retained_heads':sum(len(h['heads']) for h in audit['histories']),'already_linked_heads':sum(bool(e['parent_episode_id']) for h in audit['histories'] for e in h['heads']),'accepted_merges':0,'pending_raw_events':0,'cause':'No project has two retained heads with the same recognized affirmative source identity. All retained heads are standalone; idle does not mean baseline already consolidated them. No source budget omission or completed reconsideration no-op is needed to explain these results.','positive_reopening_gap':'Dev01 uses an explicit human reopening with a descriptive same timing repair suffix. This was intentionally left outside the conservative grammar before live development. Its source exists and is cited, but the identity predicate rejects it, so consolidation never reaches a generator or new source-grounding validation.','effectiveness_limit':'These development candidates do not exercise repaired generation, source-grounded material field assembly or accepted merging. Retrieval changes can still affect existing legacy records; generator/merge effectiveness cannot be inferred from idle jobs.'}
out=Path('docs/evidence/consolidation-repair-2026-10-20/development-opportunities.json');out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(audit,indent=2)+'\n')
print(out)
for h in audit['histories']:
 print(h['history_id'],[(p['arm'],p['project_key'],p['current_head_count'],p['coverage'].get('reconsideration'),p['coverage'].get('pending_events')) for p in h['projects']])
