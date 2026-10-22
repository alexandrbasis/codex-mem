#!/usr/bin/env python3
"""Synthetic future fidelity and configurable public-budget consumer controls."""
import argparse,ast,copy,hashlib,importlib.util,json,tempfile,unittest
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);ARGS=parser.parse_args();B=Path(__file__).resolve().parent;spec=importlib.util.spec_from_file_location('r21_held_helper',B/'held-helper-source.py');h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
class Fidelity(unittest.TestCase):
 def protocol(self):return dict(model='gpt-6.1-sol',reasoning_effort='medium',reader_fidelity_contract='quantified-source-fidelity-v1',reader_evidence_contract='assertion-owned-citations-v1',reader_context_isolation='per-question-v1',reader_citation_contract='typed-source-fields-v1',context_chars=6000,reader_profile=dict(model='gpt-6.1-sol',reasoning_effort='medium'))
 def test_fidelity_prompt_and_schema(self):
  p=self.protocol();p['reader_citation_contract']=h.READER_CITATION_CONTRACT
  self.assertEqual(h.reader_prompt_version(p),'role-attribution-per-question-assertions-fidelity-v1')
  context=dict(id='q',question='Synthetic question?',memory='<project-memory/>',context_chars=17)
  fidelity=h.typed_reader_request([context],[],p);old=dict(p);del old['reader_fidelity_contract'];legacy=h.typed_reader_request([context],[],old)
  self.assertEqual(fidelity['output_schema'],legacy['output_schema']);self.assertNotEqual(fidelity['prompt'],legacy['prompt']);self.assertIn('Retain explicit quantifiers',fidelity['prompt'])
 def test_invalid_fidelity_and_budget_rejected(self):
  for key,value in [('reader_fidelity_contract','unknown'),('reader_evidence_contract','unknown'),('context_chars',5999),('reader_context_isolation','unknown')]:
   p=self.protocol();p['reader_citation_contract']=h.READER_CITATION_CONTRACT;p[key]=value
   with self.assertRaises(ValueError):h.reader_prompt_version(p)
class BudgetConsumer(unittest.TestCase):
 def run_budget(self,count):
  source=ARGS.source.read_text();main=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='main');start=next(i for i,n in enumerate(main.body) if isinstance(n,ast.Assign) and ast.get_source_segment(source,n).startswith('budget_path ='));end=next(i for i,n in enumerate(main.body[start:],start) if isinstance(n,ast.Assign) and ast.get_source_segment(source,n).startswith('freeze ='));code=compile(ast.fix_missing_locations(ast.Module(body=main.body[start:end],type_ignores=[])),'actual-budget-consumer','exec')
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);budget=dict(public_native_calls=10,limits=dict(max_calls=420),planned_call_allocation=dict(public_readers=8,public_judges=2));raw=json.dumps(budget).encode();(root/'budget.json').write_bytes(raw);checks=[];binding=dict(public_calls=count,budget_binding=dict(path='budget.json',sha256=hashlib.sha256(raw).hexdigest()),public_allocation_keys=['public_readers','public_judges'])
   exec(code,dict(repo=root,binding=binding,protocol=budget,sha=lambda v:hashlib.sha256(v).hexdigest(),read=lambda p:json.loads(p.read_bytes()),check=lambda v,k:checks.append((k,v))))
   return checks
 def test_ten_calls_valid(self):self.assertTrue(all(v for k,v in self.run_budget(10)))
 def test_eight_calls_rejected(self):self.assertIn(('explicit_public_call_count',False),self.run_budget(8));self.assertIn(('protocol_public_call_budget',False),self.run_budget(8))
if __name__=='__main__':unittest.main(argv=['test-r21-contracts-v002.py'])
