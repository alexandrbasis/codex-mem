#!/usr/bin/env python3
"""Execute the owning main path with synthetic gate files and ledger values.

This is an executable main-path slice, not a name/AST assertion. It executes
unchanged source statements that load CLI bindings, traverse final review gates,
and consume public_tokens. Other main stages require a future released export.
"""
import argparse,ast,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
P=argparse.ArgumentParser();P.add_argument('--source',type=Path,required=True);ARGS=P.parse_args();SOURCE=ARGS.source.read_text()
class MainBindingPath(unittest.TestCase):
 def test_review_gates_do_not_replace_cli_trial_binding(self):
  main=next(n for n in ast.parse(SOURCE).body if isinstance(n,ast.FunctionDef) and n.name=='main')
  load=next(n for n in main.body if isinstance(n,ast.Assign) and 'args.binding_json.read_bytes()' in ast.get_source_segment(SOURCE,n))
  gate=next(n for n in main.body if isinstance(n,ast.For) and 'final_gate_bindings' in ast.get_source_segment(SOURCE,n))
  consume=next(n for n in main.body if isinstance(n,ast.Expr) and 'public_usage_included_once' in ast.get_source_segment(SOURCE,n))
  path=ast.fix_missing_locations(ast.Module(body=[load,gate,consume],type_ignores=[]))
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'review.json').write_text('synthetic independent review')
   (root/'binding.json').write_text(json.dumps({'public_calls':10,'public_tokens':1000,'helper_sha256':'held-helper'}))
   checks=[]
   env={'json':json,'Path':Path,'args':SimpleNamespace(binding_json=root/'binding.json'),'repo':root,'preparation':{'final_gate_bindings':{'spec':{'path':'review.json','sha256':'synthetic-review-hash'},'standards':{'path':str(root/'review.json'),'sha256':'synthetic-review-hash'}}},'sha':lambda _: 'synthetic-review-hash','check':lambda value,label:checks.append((label,value)),'public':list(range(10)),'finishes':{i:{'metrics':{'usage':{'tokens':{'total_tokens':100}}}} for i in range(10)}}
   exec(compile(path,str(ARGS.source),'exec'),env)
   self.assertEqual(checks,[('final_gate_binding:spec',True),('final_gate_binding:standards',True),('public_usage_included_once',True)])
   self.assertEqual(env['binding']['helper_sha256'],'held-helper')
if __name__=='__main__':unittest.main(argv=['test-main-binding-path.py'])
