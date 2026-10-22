"""Replay disclosed development source availability without model calls or rescoring.

Example: PYTHONPATH=. python3 docs/evidence/project-memory-quality-2026-10-21-r2/replay-development-sources.py --run RUN --fixture DEVELOPMENT.json --gold DEVELOPMENT-gold.json --output REPLAY.json
"""
import argparse
import sys
import json, pathlib, sqlite3, tempfile, shutil, hashlib, xml.etree.ElementTree as ET
REPO = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from codex_mem.store import Store
from codex_mem.project_memory_retrieval import build_project_brief, render_project_brief
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--run', type=pathlib.Path, required=True)
parser.add_argument('--fixture', type=pathlib.Path, required=True)
parser.add_argument('--gold', type=pathlib.Path, required=True)
parser.add_argument('--output', type=pathlib.Path, required=True)
args = parser.parse_args()
ROOT, FIX, GOLD, OUT = (path.resolve() for path in (args.run, args.fixture, args.gold, args.output))
def load(p):return json.loads(p.read_text())
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def refs(contexts):
 result=[]
 for q in contexts:
  # Every fixture question selects one project. Keep its canonical XML intact.
  text=q['memory']
  if '<project-memory' not in text:continue
  xml=text[text.index('<project-memory'):]
  tree=ET.fromstring(xml)
  for item in tree.findall('item'):
   for field in item.findall('field'):
    for source in field.findall('source'):
     quote=source.findtext('quote_excerpt')
     if quote is not None:result.append({'query_view':q['id'],'lane':item.get('lane'),'field':field.get('name'),**source.attrib,'quote':quote})
 return result
assert load(FIX).get('split') == 'development', 'Only disclosed development fixtures are supported'
assert load(GOLD).get('split') == 'development', 'Only disclosed development gold is supported'
histories=[]; losses=[]; maxview=0
for history in load(FIX)['histories']:
 hid=history['id']; generation=load(ROOT/'candidate'/hid/'generation.json')
 tracked=[ROOT/arm/hid/name for arm in ['baseline','candidate'] for name in ['contexts.json','citation-catalog.json','reader-0.json','judge-0.json','data/memory.sqlite3']]
 before={str(p):sha(p) for p in tracked}
 with tempfile.TemporaryDirectory() as temp:
  data=pathlib.Path(temp); original=ROOT/'candidate'/hid/'data'
  shutil.copyfile(original/'config.json',data/'config.json')
  source=sqlite3.connect('file:'+str(original/'memory.sqlite3')+'?mode=ro',uri=True)
  target=sqlite3.connect(data/'memory.sqlite3');source.backup(target);source.close();target.close()
  contexts=[]
  with Store(data) as store:
   for question in history['questions']:
    assert len(question['project_keys'])==1
    key=question['project_keys'][0]
    brief=build_project_brief(store,generation['projects'][key],query=question['question'],budget=max(1000,6000//len(question['project_keys'])-100),include_shadow=True)
    memory='PROJECT '+key+'\n'+render_project_brief(brief,budget=max(1000,6000//len(question['project_keys'])-100))
    if brief['xml']:ET.fromstring(brief['xml'])
    assert len(memory)<=6000
    maxview=max(maxview,len(memory))
    contexts.append({'id':question['id'],'question':question['question'],'memory':memory,'context_chars':len(memory),'context_sha256':hashlib.sha256(memory.encode()).hexdigest(),'render_omissions':brief.get('render_omissions'),'skip_reason':brief.get('skip_reason')})
 newrefs=refs(contexts); baseline=load(ROOT/'baseline'/hid/'citation-catalog.json')['entries']; candidate=load(ROOT/'candidate'/hid/'citation-catalog.json')['entries']
 def visible(pool,source,role,fragment):return [r for r in pool if r['source_id']==source and r['source_role']==role and fragment in r['quote']]
 facts=[];events={e['id']:e for session in history['sessions'] for e in session['events']}
 for question in history['questions']:
  qkey=hid+'/'+question['id']
  for index,fact in enumerate(load(GOLD)['questions'][qkey]['required_facts']):
   checks=[]
   for support in fact['support']:
    eid=support['event_id'];sid=generation['source_map'][eid];role={'user':'user_intent','assistant':'assistant_report','tool':'tool_record'}[events[eid]['type']];fragment=support['fragment']
    old=visible(baseline,sid,role,fragment);previous=visible(candidate,sid,role,fragment);new=visible(newrefs,sid,role,fragment)
    check={'event_id':eid,'source_id':sid,'source_role':role,'fragment':fragment,'baseline_visible':bool(old),'previous_candidate_visible':bool(previous),'new_visible':bool(new),'new_pointers':[{k:r.get(k) for k in ['query_view','lane','field','source_field','start','end','quote']} for r in new]}
    if old and not new:losses.append({'question':qkey,'fact_index':index,**check})
    checks.append(check)
   facts.append({'question':qkey,'fact_index':index,'fact':fact['text'],'support':checks})
 def lost_quotes(old):return [r for r in old if not visible(newrefs,r['source_id'],r['source_role'],r['quote'].strip())]
 after={str(p):sha(p) for p in tracked};assert before==after
 histories.append({'history_id':hid,'database_and_original_artifact_hashes_before':before,'database_and_original_artifact_hashes_after':after,'originals_unchanged':True,'new_contexts':contexts,'new_visible_source_quotes':newrefs,'required_fact_source_availability':facts,'baseline_quote_spans_not_contained_in_new_union':lost_quotes(baseline),'previous_candidate_quote_spans_not_contained_in_new_union':lost_quotes(candidate)})
report={'schema_version':1,'purpose':'Offline source availability replay on four disclosed DEVELOPMENT common Stores; no reader/judge rerun or rescore','model_calls':0,'method':'Exact frozen development-v1-helper retrieve_question partition: per_project=max(1000,6000//len(keys)-100), build_project_brief include_shadow=True, then render_project_brief at per_project, PROJECT header and combined-context <=6000 assertion (no slicing). Each development question has one key. Supersedes direct-6000 renderer preview v2, preserved separately.','helper_sha256':sha(REPO/'docs/evidence/project-memory-quality-2026-10-21-r2/development-v1-helper.py'),'budget':6000,'max_actual_view_chars':maxview,'runtime_sha256':{str(p):sha(REPO/p) for p in map(pathlib.Path,['codex_mem/project_memory_retrieval.py','codex_mem/project_memory_query.py'])},'input_paths':{'run':str(ROOT),'fixture':str(FIX),'gold':str(GOLD)},'script_sha256':sha(pathlib.Path(__file__)),'fixture_sha256':sha(FIX),'gold_sha256':sha(GOLD),'baseline_visible_required_support_lost':losses,'histories':histories,'limits':['This checks actual rendered source availability, not answer quality or a clean holdout.','Exact source IDs and roles must match, and a fragment must occur in a complete quoted pointer; labels and derived text do not establish support.','Runtime uses temporary SQLite backup copies; originals and their contexts/catalogs/grades remain unchanged.']}
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
print(json.dumps({'artifact':str(OUT),'sha256':sha(OUT),'max_view_chars':maxview,'lost_baseline_required_support':losses,'other_lost_baseline_quotes':{h['history_id']:len(h['baseline_quote_spans_not_contained_in_new_union']) for h in histories},'other_lost_candidate_quotes':{h['history_id']:len(h['previous_candidate_quote_spans_not_contained_in_new_union']) for h in histories}}))
