"""Real Capture producer to audit consumer; no native dispatch or Store."""
import copy,importlib.util,json,sys,tempfile,unittest
from pathlib import Path
B=Path(__file__).resolve().parent;sys.path.insert(0,str(B.parents[2]));from pipeline_contract import validate_checkpoints
from codex_mem.pricing import token_counts
spec=importlib.util.spec_from_file_location('checkpoint_six_source',B/'held-helper-source.py');h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
FIELDS={'input_tokens','cached_input_tokens','cache_write_input_tokens','output_tokens','reasoning_output_tokens','total_tokens'}
class SixCounters(unittest.TestCase):
 def emitted(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);run=Path(t.name);(run/'receipts').mkdir();h.write(run/'run.json',{'budget_run':str(run)});capture=h.Capture(run,{},'generation','baseline','synthetic');base={**capture.identity,'call_id':'call-0001'};tokens=dict(input_tokens=7,cached_input_tokens=2,cache_write_input_tokens=1,output_tokens=3,reasoning_output_tokens=1,total_tokens=10);capture._save_checkpoint(base,dict(worker_thread_id='synthetic-thread',worker_turn_id=None,metrics=dict(duration_ms=1,usage=dict(status='reported',source='app_server_thread_total',updates=1,tokens=tokens))));path=run/'receipts/call-0001-checkpoint.json';self.assertTrue(path.is_file());data=json.loads(path.read_text());self.assertEqual(set(data['metrics']['usage']['tokens']),FIELDS);return run,base,path,data
 def inspect(self,run,base):
  checks=[];result=validate_checkpoints(run,{'call-0001':base},lambda v,k:checks.append((k,v)));self.assertEqual(result['count'],1);self.assertTrue(checks);return checks
 def test_real_emitted_six_counter_positive(self):
  run,base,path,data=self.emitted();self.assertEqual(token_counts(data['metrics']['usage']['tokens']),data['metrics']['usage']['tokens']);self.assertTrue(all(v for k,v in self.inspect(run,base)))
 def test_wrong_attempt_negative(self):
  run,base,path,data=self.emitted();data['history_id']='different';h.write(path,data);checks=self.inspect(run,base);self.assertIn(('checkpoint_attempt_binding:call-0001',False),checks)
 def test_missing_extra_bool_negative_bounds_sum_and_subsets(self):
  variants=[]
  for name in FIELDS:
   variants.append(('missing:'+name,lambda t,n=name:t.pop(n)))
  variants.extend([('extra',lambda t:t.update(extra_tokens=0)),('bool',lambda t:t.update(input_tokens=True)),('negative',lambda t:t.update(cache_write_input_tokens=-1)),('too_large',lambda t:t.update(input_tokens=2**63)),('bad_sum',lambda t:t.update(total_tokens=11)),('cache_subsets',lambda t:t.update(cached_input_tokens=5,cache_write_input_tokens=3)),('reasoning_subset',lambda t:t.update(reasoning_output_tokens=4))])
  for name,mutate in variants:
   with self.subTest(name=name):
    run,base,path,data=self.emitted();mutate(data['metrics']['usage']['tokens']);h.write(path,data);self.assertIn(('checkpoint_token_shape:call-0001',False),self.inspect(run,base))
 def test_ids_duration_updates_bounds(self):
  variants=[('identifier',lambda d:d.update(worker_thread_id='x'*257),'checkpoint_identifier_bounds:call-0001'),('duration',lambda d:d['metrics'].update(duration_ms=2**63),'checkpoint_duration_bounds:call-0001'),('updates_bool',lambda d:d['metrics']['usage'].update(updates=True),'checkpoint_usage_schema:call-0001')]
  for name,mutate,label in variants:
   with self.subTest(name=name):
    run,base,path,data=self.emitted();mutate(data);h.write(path,data);self.assertIn((label,False),self.inspect(run,base))
if __name__=='__main__':unittest.main()
