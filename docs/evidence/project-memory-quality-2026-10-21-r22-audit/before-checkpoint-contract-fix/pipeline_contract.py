"""Independent shared-pipeline receipts and diagnostic checkpoint audit. Read-only."""
import json,re
from pathlib import Path
def read(path):return json.loads(path.read_bytes())
def selected(protocol,binding):
 value=binding['pipeline_receipt_contract']
 if value is None:
  if 'pipeline_receipt_contract' in protocol:raise ValueError('legacy binding cannot hide selected pipeline contract')
  return False
 if value!='complete-shared-pipeline-v1' or protocol.get('pipeline_receipt_contract')!=value or protocol.get('comparison_mode','shared-prefix')!='shared-prefix':raise ValueError('unsupported or mismatched pipeline contract')
 return True
def stage(run,arm,hid,name):
 path=run/arm/hid/(name+'.json');return read(path).get('status','unknown') if path.exists() else 'missing'
def grade_ready(run,hid,arm,run_id,contract):
 if contract is None:return True
 if contract!='complete-shared-pipeline-v1':raise ValueError('unsupported pipeline grade contract')
 home=run if run_id=='primary' else run/(run_id if run_id.startswith('replicates/') else 'replicates/'+run_id)
 arms=('baseline','candidate') if arm=='candidate' else ('baseline',)
 return all(stage(home,a,hid,'generation')=='complete' for a in arms)
def validate_pipeline(run,fixture,protocol,report,binding,check):
 if not selected(protocol,binding):return {'contract':None,'scope':'explicit legacy mode'}
 ids=[h['id'] for h in fixture['histories']];generation=[];prep=[]
 for hid in ids:
  generation.append(dict(variant='baseline',history_id=hid,stage='generation',status=stage(run,'baseline',hid,'generation')))
  path=run/'candidate'/hid/'consolidation.json'
  if path.exists():
   c=read(path);prep.append(dict(variant='candidate',stage='consolidation',history_id=hid,status=c['status'],accepted_merge_count=c.get('accepted_merge_count',0),accepted_episode_ids=[x['id'] for x in c.get('accepted_merge_records',[])],job_kinds=[j.get('job_kind','raw_observation') for j in c['jobs'] if j['status']!='idle']))
 regen=[]
 for hid in ids:
  if hid not in protocol['regeneration_repeat_history_ids']:continue
  for arm,name in [('baseline','generation'),('candidate','consolidation')]:regen.append(dict(run_id='regeneration-1',history_id=hid,variant=arm,stage=name,status=stage(run/'replicates/regeneration-1',arm,hid,name)))
 failures=[x for x in prep+generation if x['status']!='complete']
 failures += [dict(variant='candidate',history_id=hid,stage='consolidation',status='missing') for hid in ids if not (run/'candidate'/hid/'consolidation.json').exists()]
 failures += [x for x in regen if x['status']!='complete']
 check(report['pipeline_receipt_contract']==binding['pipeline_receipt_contract'],'pipeline_contract_binding')
 check(report['generation_receipts']==generation,'physical_generation_exact_once')
 check(report['consolidation_receipts']==prep,'candidate_preparation_receipts')
 check(report['regeneration_pipeline_receipts']==regen,'required_regeneration_receipts_including_missing')
 check(report['pipeline_failures']==failures,'complete_pipeline_failure_recount')
 check(not (failures and report['decision']=='pass'),'pipeline_failure_blocks_pass')
 return dict(contract=binding['pipeline_receipt_contract'],generation=generation,preparation=prep,regeneration=regen,failures=failures)
def validate_checkpoints(run,starts,check):
 count=0
 for path in sorted((run/'receipts').glob('call-*-checkpoint.json')):
  c=read(path);cid=c['call_id'];check(cid in starts,'checkpoint_known_attempt:'+cid)
  if cid not in starts:continue
  start=starts[cid];metadata=set(c)-{'schema_version','worker_thread_id','worker_turn_id','metrics'}
  allowed=set(start)-{'status','at','prompt_chars','request_sha256','request_bytes_sha256'}
  check(c['schema_version']=='native-usage-checkpoint-v1' and metadata<=allowed and {'call_id','phase'}<=metadata,'checkpoint_schema:'+cid)
  check(all(c[k]==start[k] for k in metadata),'checkpoint_attempt_binding:'+cid)
  check(isinstance(c['worker_thread_id'],str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,256}',c['worker_thread_id']) is not None and (c['worker_turn_id'] is None or isinstance(c['worker_turn_id'],str) and re.fullmatch(r'[A-Za-z0-9._:-]{1,256}',c['worker_turn_id']) is not None),'checkpoint_identifier_bounds:'+cid)
  m=c['metrics'];u=m['usage'];check(set(m)=={'duration_ms','usage'} and type(m['duration_ms']) is int and 0<=m['duration_ms']<=2**63-1,'checkpoint_duration_bounds:'+cid)
  check(set(u)=={'status','source','updates','tokens'} and u['status'] in ('reported','partial','invalid','unavailable') and u['source']=='app_server_thread_total' and type(u['updates']) is int and 0<=u['updates']<=2**63-1,'checkpoint_usage_schema:'+cid)
  tokens=u['tokens'];valid=tokens is None if u['status'] in ('invalid','unavailable') else (isinstance(tokens,dict) and set(tokens)=={'input_tokens','output_tokens','cached_input_tokens','total_tokens'} and all(type(v) is int and v>=0 for v in tokens.values()) and tokens['total_tokens']==tokens['input_tokens']+tokens['output_tokens'] and tokens['cached_input_tokens']<=tokens['input_tokens'])
  check(valid,'checkpoint_token_shape:'+cid);count+=1
 return {'count':count,'scope':'Bounded diagnostic receipts only. Authoritative final usage comes exclusively from native ledger finishes; checkpoints never fill unknown totals.'}
