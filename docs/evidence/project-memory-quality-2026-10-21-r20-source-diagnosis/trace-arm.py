"""Read frozen contexts and replay only scratch SQLite backups. No model calls."""
import sys,os,json,sqlite3,tempfile,shutil,inspect,hashlib
from pathlib import Path
from dataclasses import asdict
ARM=sys.argv[1];RUNTIME=Path(sys.argv[2]);OUT=Path(sys.argv[3]);RUN=Path('/private/tmp/codex-mem-quality-20261021-r20-experiment')
sys.dont_write_bytecode=True;sys.path.insert(0,str(RUNTIME))
from codex_mem.store import Store
from codex_mem.project_memory_retrieval import build_project_brief,render_project_brief
from codex_mem.project_memory_query import plan_query
FIX=json.load(open('/private/tmp/codex-mem-quality-20261021-r20-sealed/holdout-fixture.json'))
H={h['id']:h for h in FIX['histories']}
keys={('h03','q2'),('h05','q5'),('h12','q5')}
def serial(value):
 if isinstance(value,dict):return {str(k):serial(v) for k,v in value.items()}
 if isinstance(value,(list,tuple,set)):return [serial(x) for x in value]
 if isinstance(value,Path):return str(value)
 if isinstance(value,(str,int,float,bool)) or value is None:return value
 return str(value)
rows=[]
for case,qid in sorted(keys):
 original=RUN/ARM/case;database=original/'data/memory.sqlite3';database_before=hashlib.sha256(database.read_bytes()).hexdigest();gen=json.load(open(original/'generation.json'));saved=next(c for c in json.load(open(original/'contexts.json')) if c['id']==qid)
 scopes=saved['selected_scope_context']['selected_project_scopes'];parts=[];owners=[]
 with tempfile.TemporaryDirectory(prefix='r19-diagnosis-'+ARM+'-') as scratch:
  data=Path(scratch)/'data';data.mkdir();shutil.copy2(original/'data/config.json',data/'config.json')
  connection=sqlite3.connect('file:'+str(original/'data/memory.sqlite3')+'?mode=ro',uri=True)
  with sqlite3.connect(data/'memory.sqlite3') as target:connection.backup(target)
  connection.close()
  for project in scopes:
   trace={'scope_checks':[],'source_pool_calls':[],'snapshot_calls':[],'render_calls':[]}
   def tracer(frame,event,arg):
    if frame.f_code.co_filename.endswith('project_memory_retrieval.py') and frame.f_code.co_name=='_plan_original_source_transport' and event=='line':
     loc=frame.f_locals
     if frame.f_lineno in {929,930,931,932,933,963,964,965,966,967,968,969,970} and loc.get('group'):
      trace.setdefault('packing_steps',[]).append({'line':frame.f_lineno,'source_ids':[r.get('source_id') for r in loc['group']['refs']], 'neutral':loc.get('is_neutral'),'cost':loc.get('cost'),'available':loc.get('available'),'neutral_spent':loc.get('neutral_spent'),'neutral_limit':loc.get('neutral_limit'),'neutral_selected':loc.get('neutral_selected'),'marker_chars':len(loc.get('marker',''))})
    if event!='return':return tracer
    name=frame.f_code.co_name;loc=frame.f_locals
    if frame.f_code.co_filename.endswith('project_memory_retrieval.py'):
     if name=='scoped':trace['scope_checks'].append({'value':serial(loc.get('value')),'accepted':bool(arg)})
     elif name=='_source_evidence':trace['source_pool_calls'].append({'unassociated':loc.get('unassociated_context',False),'result':serial(arg),'expressions':serial(loc.get('expressions'))})
     elif name=='_complete_source_groups':trace.setdefault('complete_groups',[]).append({'user_only':loc.get('user_only'),'result':serial(arg)})
     elif name=='_render_project_brief':trace['render_calls'].append({'omissions':serial(arg[1]) if isinstance(arg,tuple) else None,'characters':len(arg[0]) if isinstance(arg,tuple) else None})
     elif name=='build_project_brief':trace['builder_locals']={k:serial(loc.get(k)) for k in ['query','fallback_used','configured','mode','environment','version','supplement_allowed','records','snapshot'] if k in loc}
    elif frame.f_code.co_filename.endswith('project_memory.py') and name=='snapshot':trace['snapshot_calls'].append({'query':loc.get('query'),'result':serial(arg)})
    return tracer
   kwargs={'selected_project_scopes':scopes} if 'selected_project_scopes' in inspect.signature(build_project_brief).parameters else {}
   with Store(data) as store:
    sys.settrace(tracer)
    try:
     brief=build_project_brief(store,project,query=saved['question'],budget=max(1000,6000//len(scopes)-100),include_shadow=True,**kwargs)
     xml=render_project_brief(brief,budget=max(1000,6000//len(scopes)-100))
    finally:sys.settrace(None)
   pkwargs={'selected_project_scopes':scopes} if 'selected_project_scopes' in inspect.signature(plan_query).parameters else {}
   plan=plan_query(saved['question'],project,**pkwargs)
   key=next(k for k,v in gen['projects'].items() if v==project);parts.append('PROJECT '+key+'\n'+xml)
   owners.append({'project':project,'project_key':key,'plan':asdict(plan),'brief':serial(brief),'trace':trace})
  rebuilt='\n\n'.join(parts)
  assert rebuilt==saved['memory'],(ARM,case,qid,'replay_not_byteexact')
  # Read canonical active source bodies from original database only via mode=ro.
  con=sqlite3.connect('file:'+str(original/'data/memory.sqlite3')+'?mode=ro',uri=True);con.row_factory=sqlite3.Row
  entries=[dict(x) for x in con.execute('SELECT * FROM entries ORDER BY project,id')];con.close()
 database_after=hashlib.sha256(database.read_bytes()).hexdigest();assert database_before==database_after
 rows.append({'database_sha256_before':database_before,'database_sha256_after':database_after,'case':case,'id':qid,'question':saved['question'],'saved_context':saved,'replay_byteexact':True,'owners':owners,'canonical_entries':entries,'generation_source_map':gen['source_map']})
OUT.write_text(json.dumps({'arm':ARM,'runtime_root':str(RUNTIME),'store_original_writes':0,'models':0,'rows':rows},indent=2)+'\n')
print(ARM,'views',len(rows),'byteexact',sum(x['replay_byteexact'] for x in rows),'sha256',hashlib.sha256(OUT.read_bytes()).hexdigest())
