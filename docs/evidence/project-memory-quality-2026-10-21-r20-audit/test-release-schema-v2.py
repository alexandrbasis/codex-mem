#!/usr/bin/env python3
"""Synthetic release archive end-to-end plus actual main release-consumer slice."""
import argparse,ast,hashlib,io,json,tarfile,tempfile,unittest
from pathlib import Path
from release_schema_v2 import validate_release
p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);args=p.parse_args()
sha=lambda data:hashlib.sha256(data).hexdigest()
class ReleaseSchema(unittest.TestCase):
 def test_actual_main_flat_freeze_consumer(self):
  source=args.source.read_text();main=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='main');node=next(n for n in main.body if isinstance(n,ast.Expr) and 'actual_freeze_release_binding' in ast.get_source_segment(source,n));checks=[]
  with tempfile.TemporaryDirectory() as t:
   run=Path(t);(run/'freeze.json').write_bytes(b'synthetic freeze');env=dict(run=run,release={'actual_freeze_sha256':sha(b'synthetic freeze')},sha=sha,check=lambda v,k:checks.append((k,v)))
   exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(args.source),'exec'),env);self.assertEqual(checks,[('actual_freeze_release_binding',True)])
 def fixture(self,root):
  base=root/'public';author=root/'author';run=root/'run'
  for d in (base,author,run):d.mkdir()
  (run/'freeze.json').write_bytes(b'freeze');(base/'protocol.json').write_bytes(b'protocol');(base/'contract.py').write_bytes(b'public contract')
  members={f'root-{i}.json':b'synthetic authored content' for i in range(21)}|{f'reviewer/item-{i}.json':b'synthetic reviewer content' for i in range(10)}
  counts={'histories':16,'questions':96,'required_facts':169,'positive_anchors':169}
  manifest={'files':{n:sha(v) for n,v in members.items()},'generic_contract_pins':{'contract.py':sha(b'public contract')},'counts':counts};members['manifest.json']=json.dumps(manifest).encode()
  for n,data in members.items():p=author/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
  with tarfile.open(base/'released-author-evidence.tar','w') as t:
   for n,data in members.items():m=tarfile.TarInfo(n);m.size=len(data);t.addfile(m,io.BytesIO(data))
  release=dict(runtime_pin_count=48,archive_member_type='regular_files_only',status='formally_released_after_pinned_freeze_and_native_public_gate',actual_freeze_sha256=sha(b'freeze'),protocol_canonical_digest='protocol-digest',helper_sha256='held-helper',independent_review_status='accepted',unresolved_findings=0,all_preserved_and_frozen_input_bytes_unchanged=True,prerequisite_pins={'protocol.json':sha(b'protocol')},archive_sha256=sha((base/'released-author-evidence.tar').read_bytes()),archive_member_count=32,archive_members_and_hashes={n:sha(v) for n,v in members.items()},manifest_basename='manifest.json',manifest_sha256=sha(members['manifest.json']),preserved_manifest_file_count=31,verified_frozen_root_pin_count=22,counts=counts)
  return base,author,run,release
 def run_fixture(self,mutate=None):
  with tempfile.TemporaryDirectory() as t:
   base,author,run,release=self.fixture(Path(t))
   if mutate:mutate(base,author,run,release)
   checks=[];result=validate_release(base,author,run,release,'protocol-digest','held-helper',lambda v,k:checks.append((k,v)));return result,checks
 def test_full_synthetic_release_archive(self):
  result,checks=self.run_fixture();self.assertEqual(len(result),32);self.assertTrue(all(v for k,v in checks))
 def test_wrong_manifest_count_rejected(self):
  _,checks=self.run_fixture(lambda b,a,r,x:x.update(preserved_manifest_file_count=30));self.assertIn(('author_31_preserved_files',False),checks)
 def test_wrong_member_hash_rejected(self):
  _,checks=self.run_fixture(lambda b,a,r,x:x['archive_members_and_hashes'].update({'root-0.json':'wrong'}));self.assertIn(('author_exact_member_hashes',False),checks)
 def test_mutated_private_original_rejected(self):
  _,checks=self.run_fixture(lambda b,a,r,x:(a/'root-0.json').write_bytes(b'changed'));self.assertIn(('released_author_private_identity:root-0.json',False),checks)
if __name__=='__main__':unittest.main(argv=['test-release-schema-v2.py'])
