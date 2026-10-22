#!/usr/bin/env python3
"""Executable partial-export consumers only; no full integrity CLI or DB opens."""
import ast,collections,copy,hashlib,importlib.util,json,tarfile,unittest
from pathlib import Path
import jsonschema
B=Path(__file__).resolve().parent;S=B.parent/'project-memory-quality-2026-10-21-r21';E=S/'holdout';R=Path('/private/tmp/codex-mem-quality-20261021-r21-experiment');source=(B/'audit-final-export-v003.py').read_text();main=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='main')
read=lambda p:json.loads(p.read_bytes());sha=lambda data:hashlib.sha256(data).hexdigest()
spec=importlib.util.spec_from_file_location('held_r21_partial_test_helper',S/'frozen-helper.py');h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
with tarfile.open(E/'fictional-run-evidence.tar.gz') as t:members={m.name:t.extractfile(m).read() for m in t.getmembers() if m.isfile()}
ledger=[json.loads(x) for x in members['calls.jsonl'].splitlines()]
class PartialConsumers(unittest.TestCase):
 def test_actual_55_ledger_request_response_profile_schema_consumers(self):
  start=next(i for i,n in enumerate(main.body) if isinstance(n,ast.Assign) and ast.get_source_segment(source,n).startswith('ledger ='));end=next(i for i,n in enumerate(main.body[start:],start) if isinstance(n,ast.Assign) and ast.get_source_segment(source,n).startswith('cleaned, _ ='));checks=[]
  env=dict(members=members,run=R,helper=h,helper_hash=sha((S/'frozen-helper.py').read_bytes()),binding=read(B/'actual-audit-binding.json'),protocol=read(S/'protocol.json'),json=json,collections=collections,jsonschema=jsonschema,sha=sha,check=lambda v,k:checks.append((k,v)))
  exec(compile(ast.fix_missing_locations(ast.Module(body=main.body[start:end],type_ignores=[])),'actual-partial-native-consumers','exec'),env)
  self.assertTrue(all(v for k,v in checks),[k for k,v in checks if not v]);self.assertEqual(len(env['starts']),55);self.assertEqual(env['native_schema_checks'],54);self.assertEqual(len(env['unknown_usage_calls']),1);self.assertEqual(env['token_sums']['total_tokens'],770655)
 def coverage(self,records,report):
  start=next(i for i,n in enumerate(main.body) if isinstance(n,ast.Assign) and ast.get_source_segment(source,n).startswith('coverage, grades,'));end=next(i for i,n in enumerate(main.body[start:],start) if isinstance(n,ast.Import) and any(x.name=='sqlite3' for x in n.names));nodes=main.body[start:end];nodes=nodes+[next(n for n in main.body if isinstance(n,ast.Expr) and 'pass_not_claimed_with_failed_gate' in ast.get_source_segment(source,n))];checks=[]
  starts={r['call_id']:r for r in ledger if r['status']=='started'};finishes={r['call_id']:r for r in ledger if r['status'] in ('completed','failed')}
  env=dict(collections=collections,records=records,report=report,fixture=read(E/'fixture.json'),protocol=read(S/'protocol.json'),member=lambda n:json.loads(members[n]),starts=starts,finishes=finishes,observed_usage=report['usage'],unknown_usage_calls=['call-0055'],check=lambda v,k:checks.append((k,v)))
  exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),'actual-partial-coverage-consumers','exec'),env);return env,checks
 def test_actual_all216_missing_slots_retained(self):
  env,checks=self.coverage(read(E/'per-question.json')['questions'],read(E/'report.json'));self.assertTrue(all(v for k,v in checks));self.assertEqual(sum(env['coverage'].values()),216);self.assertEqual(len(env['grades']),0)
 def test_missing_planned_slot_fails_check(self):
  records=read(E/'per-question.json')['questions'];records[0]['attempts'].pop();_,checks=self.coverage(records,read(E/'report.json'));self.assertIn(('all_216_planned_logical_slots',False),checks)
 def test_incomplete_cannot_be_promoted_to_pass(self):
  report=read(E/'report.json');report['decision']='pass';_,checks=self.coverage(read(E/'per-question.json')['questions'],report);self.assertIn(('pass_not_claimed_with_failed_gate',False),checks)
if __name__=='__main__':unittest.main()
