import sys,json,pathlib,sqlite3,threading,re,xml.etree.ElementTree as ET,collections
sys.path.insert(0,str(pathlib.Path.cwd()))
from codex_mem.store import Store
from codex_mem.project_memory_retrieval import build_project_brief
R=pathlib.Path('/private/tmp/codex-mem-quality-20261021-r10-experiment');F=json.load(open('/private/tmp/codex-mem-quality-20261021-r10-sealed/holdout-fixture.json'));G=json.load(open('/private/tmp/codex-mem-quality-20261021-r10-sealed/holdout-gold.json'))['questions']
def refs(xml):
 out=[]
 for block in re.findall(r'<project-memory\b.*?</project-memory>',xml,re.S):
  for s in ET.fromstring(block).iter('source'):
   q=s.findtext('quote_excerpt')
   if q is not None:out.append((s.get('source_id'),s.get('source_field','body'),int(s.get('start')),int(s.get('start'))+len(q)))
 return out
def covered(rs,sid,l,r,field='body'):
 for _,_,a,b in sorted(x for x in rs if x[0]==sid and x[1]==field):
  if a>l:break
  l=max(l,b)
 return l>=r
counts=collections.Counter();gains=[];losses=[];spanlosses=[];sizes=[]
for h in F['histories']:
 p=R/'candidate'/h['id'];ge=json.load(open(p/'generation.json'));C={x['id']:x for x in json.load(open(p/'contexts.json'))};s=Store.__new__(Store);s.data_dir=p/'data';s.db_path=s.data_dir/'memory.sqlite3';s._closed=False;s._lock=threading.RLock();s._conn=sqlite3.connect(f'file:{s.db_path}?mode=ro',uri=True);s._conn.row_factory=sqlite3.Row;s._conn.execute('pragma query_only=on')
 for q in h['questions']:
  keys=q.get('project_keys',[q['project_key']]if'project_key'in q else list(ge['projects']));sc=[ge['projects'][k]for k in keys];new='\n\n'.join('PROJECT '+k+'\n'+build_project_brief(s,ge['projects'][k],query=q['question'],budget=max(1000,6000//len(keys)-100),include_shadow=True,selected_project_scopes=sc)['xml']for k in keys);old=C[q['id']]['memory'];a,b=refs(old),refs(new);sizes.append([h['id']+'/'+q['id'],len(old),len(new)])
  for sid,field,l,r in set(a):
   if not covered(b,sid,l,r,field):spanlosses.append([h['id']+'/'+q['id'],sid,field,l,r])
  for i,f in enumerate(G[h['id']+'/'+q['id']]['required_facts']):
   oldok=all(covered(a,ge['source_map'][x['event_id']],x['start'],x['end'])for x in f['support']);newok=all(covered(b,ge['source_map'][x['event_id']],x['start'],x['end'])for x in f['support']);counts['old_full']+=oldok;counts['new_full']+=newok;counts['facts']+=1
   if newok and not oldok:gains.append([h['id']+'/'+q['id'],i])
   if oldok and not newok:losses.append([h['id']+'/'+q['id'],i])
 s._conn.close();s._closed=True
print(json.dumps(dict(counts=counts,gains=gains,losses=losses,old_visible_span_losses=spanlosses,sizes=sizes),indent=2))
