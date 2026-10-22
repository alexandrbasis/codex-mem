"""Synthetic tests only. No private trial inputs, Store, native runner or models."""
import copy,importlib.util,sys,unittest,tempfile,json,hashlib,ast
from pathlib import Path
from audit_contracts import cumulative_recount,selected_historical_usage,canonical_slots
B=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('held_r20_helper',B/'held-helper-preparation.py');h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
def usage(known=True):
 return dict(attempted_calls=1,finished_calls=1,failed_calls=0,unresolved_calls=0,calls_with_reported_tokens=int(known),calls_with_unknown_or_partial_tokens=int(not known),token_accounting_complete=known,pricing_complete=False,cost_usd=None,tokens=dict(input_tokens=8 if known else None,cached_input_tokens=0 if known else None,output_tokens=2 if known else None,total_tokens=10 if known else None),reported_tokens_lower_bound=dict(input_tokens=8,cached_input_tokens=0,output_tokens=2,total_tokens=10),hypothetical_api_equivalent_usd=dict(standard='0.1',fast='0.2'))
class Contracts(unittest.TestCase):
 def test_inherited_unknown_not_total(self):
  x=cumulative_recount(usage(False),usage());self.assertIsNone(x['tokens']['total_tokens']);self.assertEqual(x['reported_tokens_lower_bound']['total_tokens'],20);self.assertFalse(x['token_accounting_complete'])
 def test_current_unknown_not_zero(self):
  x=cumulative_recount(usage(),usage(False));self.assertIsNone(x['tokens']['total_tokens']);self.assertEqual(x['calls_with_unknown_or_partial_tokens'],1)
 def test_complete_sum(self):self.assertEqual(cumulative_recount(usage(),usage())['tokens']['total_tokens'],20)
 def packet(self):
  checks={k:dict(text='Auxiliary only',citation_refs=['R0002']) for k in ('accepted_results','open_work_and_reasons','source_role_distinctions')}
  return {'answers':{'q':dict(assertions=[dict(text='Delivered.',citation_refs=['R0001'])],uncertainties=[],evidence_check=checks)}}
 def catalog(self):return [dict(ref='R0001',source_id='s1',source_field='body',start=0,end=4,quote='one.'),dict(ref='R0002',source_id='s2',source_field='body',start=0,end=4,quote='two.')]
 def test_auxiliary_not_answer_credit(self):
  out=h.resolve_assertion_reader_output(self.packet(),[dict(id='q')],self.catalog());self.assertEqual([c['catalog_ref'] for c in out['answers'][0]['citations']],['R0001'])
 def test_intervals(self):self.assertEqual(h.reader_assertion_ownership(self.packet())['q'][0],dict(start=0,end=10,text='Delivered.',citation_refs=['R0001']))
 def test_unowned_assertion_rejected(self):
  p=self.packet();p['answers']['q']['assertions'][0]['citation_refs']=[]
  with self.assertRaises(ValueError):h.resolve_assertion_reader_output(p,[dict(id='q')],self.catalog())
class R20Selectors(unittest.TestCase):
 def test_all_other_helper_functions_byte_identical(self):
  old=(B.parent/'project-memory-quality-2026-10-21-r19/frozen-helper.py').read_text();new=(B/'held-helper-preparation.py').read_text()
  def functions(s):
   lines=s.splitlines(keepends=True);return {n.name:''.join(lines[n.lineno-1:n.end_lineno]) for n in ast.parse(s).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
  a,b=functions(old),functions(new);self.assertEqual(set(a),set(b));self.assertEqual([k for k in a if a[k]!=b[k]],['historical_accounting'])
 def test_default_legacy(self):self.assertEqual(selected_historical_usage({'usage':1,'cumulative_pilot_usage':2},{}),1)
 def test_explicit_cumulative(self):self.assertEqual(selected_historical_usage({'usage':1,'cumulative_pilot_usage':2},{'usage_field':'cumulative_pilot_usage'}),2)
 def test_invalid_selector(self):
  for field in ('unknown',None,[],True):
   with self.assertRaises(ValueError):selected_historical_usage({},dict(usage_field=field))
 def test_missing_selected_field(self):
  with self.assertRaises(KeyError):selected_historical_usage({'usage':1},dict(usage_field='cumulative_pilot_usage'))
 def test_actual_helper_selector_and_lower_bound(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);report={'usage':usage(),'cumulative_pilot_usage':usage(False)};report['cumulative_pilot_usage'].update(attempted_calls=3410,finished_calls=3410,calls_with_reported_tokens=3409,failed_calls=1)
   raw=json.dumps(report).encode();(root/'report.json').write_bytes(raw);old=h.ROOT;h.ROOT=root
   try:
    pointer=dict(report='report.json',sha256=hashlib.sha256(raw).hexdigest(),usage_field='cumulative_pilot_usage')
    result=h.historical_accounting({'historical_spend':pointer},usage())['cumulative_pilot_usage'];self.assertEqual(result,cumulative_recount(report['cumulative_pilot_usage'],usage()) | {k:result[k] for k in result if k not in cumulative_recount(report['cumulative_pilot_usage'],usage())});self.assertEqual(result['attempted_calls'],3411);self.assertIsNone(result['tokens']['total_tokens'])
    for field in ('unknown',None):
     with self.assertRaises(ValueError):h.historical_accounting({'historical_spend':dict(pointer,usage_field=field)},usage())
   finally:h.ROOT=old
 def test_216_canonical_slots(self):
  histories=[dict(id=f'h{i:02}',questions=[dict(id=f'q{j}') for j in range(6)]) for i in range(16)];slots=canonical_slots(histories,['h14'],['h03']);self.assertEqual(len(slots),216);self.assertEqual(len(set(slots)),216)
 def test_deterministic_metadata_sanitization_archive(self):
  raw={'receipt.json':json.dumps({'worker_thread_id':'12345678-1234-1234-1234-123456789abc','answer':'fictional source remains'}).encode()};clean,meta=h.sanitize_export_members(raw);self.assertEqual(meta['changed_member_count'],1);self.assertNotIn(b'12345678-1234',clean['receipt.json']);self.assertIn(b'fictional source remains',clean['receipt.json']);self.assertEqual(h.export_archive_bytes(clean),h.export_archive_bytes(dict(reversed(list(clean.items())))))

if __name__=='__main__':unittest.main()
