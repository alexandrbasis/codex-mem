"""Synthetic tests only. No private trial inputs, Store, native runner or models."""
import copy,importlib.util,sys,unittest
from pathlib import Path
from audit_contracts import cumulative_recount
B=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('held_r19_helper',B.parent/'project-memory-quality-2026-10-21-r19/frozen-helper.py');h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
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
if __name__=='__main__':unittest.main()
