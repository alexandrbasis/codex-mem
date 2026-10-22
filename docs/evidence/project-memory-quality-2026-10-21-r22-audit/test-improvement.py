#!/usr/bin/env python3
"""Execute actual main improvement/gate-consumer statements with synthetic scores."""
import argparse,ast,collections,unittest
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);args=p.parse_args();source=args.source.read_text();main=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='main')
start=next(i for i,n in enumerate(main.body) if isinstance(n,ast.Assign) and 'bscore, cscore =' in ast.get_source_segment(source,n))
stop=next(i for i,n in enumerate(main.body[start:],start) if isinstance(n,ast.Import) and any(v.name=='sqlite3' for v in n.names))
statements=main.body[start:stop]
late=next((n for n in main.body if isinstance(n,ast.If) and 'bscore / n_primary >= .9' in ast.get_source_segment(source,n)),None)
if late:
 statements=statements+[late,next(n for n in main.body if isinstance(n,ast.Expr) and 'pass_not_claimed_with_failed_gate' in ast.get_source_segment(source,n))]
code=compile(ast.fix_missing_locations(ast.Module(body=statements,type_ignores=[])),str(args.source),'exec')

class ImprovementPath(unittest.TestCase):
 def run_case(self,b,c,bt,ct,valid):
  checks=[];scores=collections.Counter({('primary','baseline',0):b,('primary','candidate',0):c});tokens={'baseline':bt,'candidate':ct}
  env=dict(equivalent=tokens,scores=scores,n_primary=96,gate_blockers=[],report={'decision':'pass','incremental_processing_and_reader_tokens':tokens,'equivalent_complete_arm_tokens':tokens},protocol={'pass_criteria':{'gain_min_percentage_points_if_baseline_below_90':10},'limits':{'max_calls':360,'max_observed_tokens':7000000}},starts={},finishes={},observed_usage={'token_accounting_complete':True,'tokens':{'total_tokens':10}},unknown_usage_calls=[],check=lambda value,label:checks.append((label,value)))
  exec(code,env);self.assertEqual(checks[-1],('pass_not_claimed_with_failed_gate',valid))
 def test_actual_token_recount_preserves_unknown(self):
  node=next((n for n in main.body if isinstance(n,ast.FunctionDef) and n.name=='known_total'),None)
  if node is None:self.skipTest('prior auditor has no unknown-preserving sum seam')
  env={};exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(args.source),'exec'),env)
  total=env['known_total'];self.assertIsNone(total([None]));self.assertIsNone(total([{'metrics':{'usage':{'status':'unavailable','tokens':{'total_tokens':None}}}}]));self.assertEqual(total([{'metrics':{'usage':{'status':'reported','tokens':{'total_tokens':17}}}}]),17)
 def test_high_equal_quality_equal_cost_reject(self):self.run_case(90,90,100,100,False)
 def test_high_quality_gain_equal_cost_accept(self):self.run_case(90,91,100,100,True)
 def test_high_equal_quality_lower_cost_accept(self):self.run_case(90,90,100,99,True)
 def test_high_quality_gain_higher_cost_reject(self):self.run_case(90,91,100,101,False)
 def test_high_quality_loss_lower_cost_reject(self):self.run_case(90,89,100,99,False)
 def test_high_unknown_baseline_reject(self):self.run_case(90,91,None,99,False)
 def test_high_unknown_candidate_reject(self):self.run_case(90,91,100,None,False)
 def test_low_quality_gain_accept(self):self.run_case(70,80,100,101,True)
 def test_low_insufficient_gain_reject(self):self.run_case(70,79,100,99,False)
if __name__=='__main__':unittest.main(argv=['test-main-improvement-path.py'])
