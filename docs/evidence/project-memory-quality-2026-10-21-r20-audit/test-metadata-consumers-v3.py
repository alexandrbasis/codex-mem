#!/usr/bin/env python3
"""Read-only actual public metadata schemas and full sourcearchive consumer block.

Not a main audit: no private cases, answers, ledger, Store or SQLite opens.
"""
import argparse,ast,hashlib,json,tarfile,unittest
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);args=p.parse_args();source=args.source.read_text();main=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='main');B=Path(__file__).resolve().parent;BASE=B.parent/'project-memory-quality-2026-10-21-r20';REPO=BASE.parents[2]
sha=lambda data:hashlib.sha256(data).hexdigest()
read=lambda path:json.loads(path.read_bytes())
class MetadataConsumers(unittest.TestCase):
 def test_all_literal_metadata_paths_in_main_exist(self):
  values={k:read(BASE/n) for k,n in {'protocol':'protocol.json','freeze':'actual-freeze.json','source_archive':'frozen-source-archive.json','source_review':'review-source-manifest.json','release':'formal-release.json'}.items()}
  values.update({k:read(BASE/'holdout'/n) for k,n in {'manifest':'export-manifest.json','sanitization':'sanitization.json','report':'report.json'}.items()});values['binding']=read(B/'actual-audit-binding.json')
  examined=[]
  for node in ast.walk(main):
   if not isinstance(node,ast.Subscript):continue
   chain=[];cursor=node
   while isinstance(cursor,ast.Subscript) and isinstance(cursor.slice,ast.Constant) and isinstance(cursor.slice.value,str):chain.insert(0,cursor.slice.value);cursor=cursor.value
   if not isinstance(cursor,ast.Name) or cursor.id not in values:continue
   current=values[cursor.id]
   for key in chain:current=current[key]
   examined.append((cursor.id,chain))
  self.assertGreater(len(examined),60)
 def test_actual_whole_sourcearchive_consumer_block(self):
  start=next(i for i,n in enumerate(main.body) if isinstance(n,ast.Assign) and ast.get_source_segment(source,n).startswith("product = archive("));end=next(i for i,n in enumerate(main.body[start:],start) if isinstance(n,ast.Assign) and ast.get_source_segment(source,n).startswith('baseline = Path('));nodes=main.body[start:end];checks=[]
  def archive(path):
   with tarfile.open(path) as t:return {m.name:t.extractfile(m).read() for m in t.getmembers() if m.isfile()}
  env=dict(base=BASE,repo=REPO,freeze=read(BASE/'actual-freeze.json'),helper_hash=sha((BASE/'frozen-helper.py').read_bytes()),sha=sha,read=read,archive=archive,check=lambda condition,label:checks.append((label,condition)))
  exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),str(args.source),'exec'),env)
  self.assertTrue(all(v for k,v in checks));self.assertGreaterEqual(len(checks),98)
if __name__=='__main__':unittest.main(argv=['test-metadata-consumers-v3.py'])
