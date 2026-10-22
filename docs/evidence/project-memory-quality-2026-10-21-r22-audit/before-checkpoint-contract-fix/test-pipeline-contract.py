#!/usr/bin/env python3
"""Public toy summary/ledger/auditor checks. No models, network or Store."""
import ast,collections,copy,importlib.util,json,tempfile,types,unittest,sys
from pathlib import Path
from pipeline_contract import validate_pipeline,grade_ready,validate_checkpoints
B=Path(__file__).resolve().parent;sys.path.insert(0,str(B.parents[2]));spec=importlib.util.spec_from_file_location('r22_helper_toy',B/'held-helper-source.py');h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
class PipelineContract(unittest.TestCase):
 def toy(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);run=Path(t.name);(run/'receipts').mkdir();h.write(run/'run.json',{'budget_run':str(run)})
  value={'split':'holdout','histories':[{'id':'toy','questions':[{'id':f'q{i}'} for i in range(6)]}]};protocol={'comparison_mode':'shared-prefix','pipeline_receipt_contract':'complete-shared-pipeline-v1','reader_repeat_history_ids':['toy'],'regeneration_repeat_history_ids':['toy']};binding={'pipeline_receipt_contract':'complete-shared-pipeline-v1'}
  return run,value,protocol,binding
 def summary(self,run,value,p):return h.aggregate(types.SimpleNamespace(run=run),value,p)
 def check_report(self,run,value,p,binding,report):
  checks=[];validate_pipeline(run,value,p,report,binding,lambda v,k:checks.append((k,v)));return checks
 def test_summary_physical_once_missing_prep_regen_visible(self):
  run,value,p,b=self.toy();h.write(run/'baseline/toy/generation.json',{'status':'failed'});report=self.summary(run,value,p)
  self.assertEqual(len(report['generation_receipts']),1);self.assertEqual(len(report['regeneration_pipeline_receipts']),2);self.assertEqual(report['decision'],'incomplete');self.assertTrue(all(v for k,v in self.check_report(run,value,p,b,report)))
 def test_stale_primary_and_repeat_cannot_hide_physical_failure(self):
  run,value,p,b=self.toy();h.write(run/'baseline/toy/generation.json',{'status':'failed'});h.write(run/'candidate/toy/generation.json',{'status':'complete'});h.write(run/'candidate/toy/judge-0.json',{'grades':[{'id':f'q{i}','full_supported_answer':True} for i in range(6)]});h.write(run/'candidate/toy/judge-1.json',{'grades':[{'id':f'q{i}','full_supported_answer':True} for i in range(6)]});report=self.summary(run,value,p)
  self.assertEqual(report['variants']['candidate']['questions_graded'],0);self.assertEqual(report['variants']['candidate']['repeat_question_results'],[]);self.assertFalse(grade_ready(run,'toy','candidate','primary',b['pipeline_receipt_contract']))
 def test_stale_regeneration_rejected(self):
  run,value,p,b=self.toy();home=run/'replicates/regeneration-1';h.write(home/'baseline/toy/generation.json',{'status':'failed'});h.write(home/'candidate/toy/generation.json',{'status':'complete'});h.write(home/'candidate/toy/judge-0.json',{'grades':[{'id':'q0','full_supported_answer':True}]});report=self.summary(run,value,p);self.assertEqual(report['regeneration_repeat_results'],[]);self.assertFalse(grade_ready(run,'toy','candidate','regeneration-1',b['pipeline_receipt_contract']))
 def test_deleted_missing_failure_receipt_detected(self):
  run,value,p,b=self.toy();report=self.summary(run,value,p);report['pipeline_failures']=[];self.assertIn(('complete_pipeline_failure_recount',False),self.check_report(run,value,p,b,report))
 def test_wrong_null_independent_contracts_fail_closed(self):
  for contract in (None,'wrong'):
   run,value,p,b=self.toy();p['pipeline_receipt_contract']=contract
   with self.assertRaises(ValueError):self.summary(run,value,p)
  run,value,p,b=self.toy();p['comparison_mode']='independent-generation'
  with self.assertRaises(ValueError):self.summary(run,value,p)
 def test_auditor_actual_grade_consumer_rejects_stale_credit(self):
  run,value,p,b=self.toy();h.write(run/'baseline/toy/generation.json',{'status':'failed'});h.write(run/'candidate/toy/generation.json',{'status':'complete'});source=(B/'audit-final-export.py').read_text();main=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='main');node=next(n for n in main.body if isinstance(n,ast.For) and ast.get_source_segment(source,n).startswith('for record in records:'));grade={'id':'q0','full_supported_answer':True,'critical_errors':[]};attempt={'run_id':'primary','variant':'candidate','repeat':0,'answer':None,'grade':grade,'reader_artifact':'candidate/toy/reader-0.json'}
  env=dict(run=run,binding=b,grade_ready=grade_ready,records=[{'history_id':'toy','question':{'id':'q0'},'attempts':[attempt]}],coverage=collections.Counter(),grades={},scores=collections.Counter(),critical=collections.Counter(),seen=set(),stale_grade_artifacts=[],member=lambda _: {'grades':[grade]},check=lambda v,k:self.assertTrue(v))
  exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'actual-auditor-grade-consumer','exec'),env);self.assertEqual(len(env['grades']),0);self.assertEqual(len(env['stale_grade_artifacts']),1)
 def test_checkpoint_reported_never_fills_final_unknown(self):
  run,value,p,b=self.toy();capture=h.Capture(run,p,'generation','baseline','toy');base={**capture.identity,'call_id':'call-0001'};known={'status':'reported','source':'app_server_thread_total','updates':1,'tokens':{'input_tokens':7,'cached_input_tokens':0,'output_tokens':3,'total_tokens':10}};capture._save_checkpoint(base,{'worker_thread_id':'synthetic-thread','worker_turn_id':None,'metrics':{'duration_ms':10,'usage':known}})
  unknown={'status':'unavailable','source':'app_server_thread_total','updates':0,'tokens':None};h.append(run/'calls.jsonl',{**base,'status':'started'});h.append(run/'calls.jsonl',{**base,'status':'failed','metrics':{'usage':unknown}})
  report=self.summary(run,value,p);self.assertIsNone(report['usage']['tokens']['total_tokens']);self.assertEqual(report['usage']['reported_tokens_lower_bound']['total_tokens'],0)
  checks=[];validate_checkpoints(run,{'call-0001':base},lambda v,k:checks.append(v));self.assertTrue(all(checks))
 def test_checkpoint_wrong_attempt_metadata_rejected(self):
  run,value,p,b=self.toy();capture=h.Capture(run,p,'generation','baseline','toy');base={**capture.identity,'call_id':'call-0001'};capture._save_checkpoint(base,{'worker_thread_id':'synthetic','worker_turn_id':None,'metrics':{'duration_ms':1,'usage':{'status':'unavailable','source':'app_server_thread_total','updates':0,'tokens':None}}});path=run/'receipts/call-0001-checkpoint.json';data=json.loads(path.read_text());data['history_id']='other';h.write(path,data);checks=[];validate_checkpoints(run,{'call-0001':base},lambda v,k:checks.append((k,v)));self.assertIn(('checkpoint_attempt_binding:call-0001',False),checks)
if __name__=='__main__':unittest.main()
