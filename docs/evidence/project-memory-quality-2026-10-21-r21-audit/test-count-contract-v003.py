#!/usr/bin/env python3
"""Pinned public mechanical validator against disclosed formal R21 count inputs.

Negative controls copy evidence into temporary scratch; originals unchanged.
No semantic grading, Store, models or native execution.
"""
import argparse,ast,hashlib,json,shutil,tempfile,unittest
from pathlib import Path
from count_contract_v003 import validate_counts
P=argparse.ArgumentParser();P.add_argument('--source',type=Path,required=True);ARGS=P.parse_args();B=Path(__file__).resolve().parent;S=B.parent/'project-memory-quality-2026-10-21-r21'
sha=lambda data:hashlib.sha256(data).hexdigest();read=lambda p:json.loads(p.read_bytes())
def contract(base):return dict(contract='pinned-public-mechanical-counts-v1',validator_sha256=sha((base/'public-fixture-contract.py').read_bytes()),release_verification_sha256=sha((base/'release-verification.json').read_bytes()),fixture_sha256=sha((base/'holdout/fixture.json').read_bytes()),gold_sha256=sha((base/'holdout/gold.json').read_bytes()))
class CountContract(unittest.TestCase):
 def execute(self,base,c):
  checks=[];result=validate_counts(base,read(base/'holdout/fixture.json'),read(base/'holdout/gold.json'),read(S/'protocol.json'),read(base/'formal-release.json'),read(S/'sealed-author-manifest.json'),c,lambda v,k:checks.append((k,v)));self.assertTrue(all(v for k,v in checks));return result
 def scratch(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);base=Path(t.name);(base/'holdout').mkdir()
  for n in ['public-fixture-contract.py','release-verification.json','formal-release.json','holdout/fixture.json','holdout/gold.json']:shutil.copyfile(S/n,base/n)
  return base
 def test_owning_main_count_consumer(self):
  source=ARGS.source.read_text();main=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='main');end=next(i for i,n in enumerate(main.body) if isinstance(n,ast.Expr) and 'released_fact_support_counts' in ast.get_source_segment(source,n));start=end
  while start>0 and not isinstance(main.body[start-1],ast.For):
   if isinstance(main.body[start-1],ast.ImportFrom) and main.body[start-1].module=='count_contract_v003':start-=1;break
   if end-start>=2:break
   start-=1
  if 'positive_anchors' in ast.get_source_segment(source,main.body[end]) and 'count_evidence' not in ast.get_source_segment(source,main.body[end]):start=end
  checks=[];env=dict(base=S,fixture=read(S/'holdout/fixture.json'),gold=read(S/'holdout/gold.json'),protocol=read(S/'protocol.json'),release=read(S/'formal-release.json'),author_path=S/'sealed-author-manifest.json',binding={'count_validation':contract(S)},read=read,fact_count=218,support_count=256,check=lambda v,k:checks.append((k,v)))
  exec(compile(ast.fix_missing_locations(ast.Module(body=main.body[start:end+1],type_ignores=[])),str(ARGS.source),'exec'),env);self.assertTrue(all(v for k,v in checks));self.assertEqual(checks[-1],('released_fact_support_counts',True))
 def test_actual_pinned_mechanical_count_derivation(self):
  result=self.execute(S,contract(S));self.assertEqual(result['counts']['required_facts'],218);self.assertEqual(result['positive_anchors'],256);self.assertEqual(result['counts']['planned_answer_slots'],216)
 def test_stale_parent_evidence_pin_rejected(self):
  base=self.scratch();c=contract(base);c['release_verification_sha256']='0'*64
  with self.assertRaisesRegex(ValueError,'count_parent_evidence_pin'):self.execute(base,c)
 def test_wrong_parent_anchor_claim_rejected(self):
  base=self.scratch();data=read(base/'release-verification.json');data['positive_anchors']=255;(base/'release-verification.json').write_text(json.dumps(data));
  with self.assertRaisesRegex(ValueError,'independent_anchor_count_bindings'):self.execute(base,contract(base))
 def test_wrong_fact_claim_rejected(self):
  base=self.scratch();data=read(base/'release-verification.json');data['counts']['required_facts']=217;(base/'release-verification.json').write_text(json.dumps(data));
  with self.assertRaisesRegex(ValueError,'independent_all_parent_counts'):self.execute(base,contract(base))
 def test_missing_contract_evidence_rejected(self):
  c=contract(S);del c['fixture_sha256']
  with self.assertRaises(KeyError):self.execute(S,c)
 def test_wrong_validator_pin_rejected_before_execution(self):
  c=contract(S);c['validator_sha256']='0'*64
  with self.assertRaisesRegex(ValueError,'count_validator_pin'):self.execute(S,c)
if __name__=='__main__':unittest.main(argv=['test-count-contract-v003.py'])
