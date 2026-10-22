"""Public toy count provenance; no disclosed or fresh private trial cases."""
import hashlib,json,tempfile,unittest
from pathlib import Path
from count_contract import validate_counts
sha=lambda v:hashlib.sha256(v).hexdigest()
class CountContract(unittest.TestCase):
 def toy(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);base=Path(t.name);(base/'holdout').mkdir();fixture={'histories':[{'id':'toy','sessions':[{'events':[{'type':'user'}],'checkpoints':[]}],'questions':[{'id':'q'}]}]};gold={'questions':{'toy/q':{'required_facts':[{'support':[{'event_id':'e'}]}]}}};protocol={'reader_repeat_history_ids':[],'regeneration_repeat_history_ids':[]};counts={'histories':1,'questions':1,'planned_answer_slots':2,'sessions':1,'events':1,'deletions':0,'checkpoints':0,'max_history_events':1,'max_history_checkpoints':0,'required_facts':1,'source_roles':{'user':1}}
  source="import json\ndef validate_files(a,b):\n f=json.loads(a.read_bytes());g=json.loads(b.read_bytes());return {'status':'positive_source_anchors_representable','histories':len(f['histories']),'questions':sum(len(h['questions']) for h in f['histories']),'positive_anchors':sum(len(x['support']) for q in g['questions'].values() for x in q['required_facts']),'deletion_controls':0}\n"
  (base/'public-fixture-contract.py').write_text(source);release={'archive_sha256':'synthetic-archive','counts':{'required_facts':1}};(base/'formal-release.json').write_text(json.dumps(release));parent={'formal_release_sha256':sha((base/'formal-release.json').read_bytes()),'archive_sha256':'synthetic-archive','counts':counts,'mechanical_status':'positive_source_anchors_representable','positive_anchors':1,'deletion_controls':0};(base/'release-verification.json').write_text(json.dumps(parent));(base/'holdout/fixture.json').write_text(json.dumps(fixture));(base/'holdout/gold.json').write_text(json.dumps(gold));manifest={'generic_pack_files':{'public-fixture-contract.py':sha((base/'public-fixture-contract.py').read_bytes())},'counts':{'required_facts':1}};c={'contract':'pinned-public-mechanical-counts-v1','validator_sha256':manifest['generic_pack_files']['public-fixture-contract.py'],'release_verification_sha256':sha((base/'release-verification.json').read_bytes()),'fixture_sha256':sha((base/'holdout/fixture.json').read_bytes()),'gold_sha256':sha((base/'holdout/gold.json').read_bytes())};return base,fixture,gold,protocol,release,manifest,c
 def test_public_toy_independent_counts(self):
  args=self.toy();checks=[];out=validate_counts(*args,lambda v,k:checks.append(v));self.assertTrue(all(checks));self.assertEqual(out['positive_anchors'],1)
 def selected(self):
  args=list(self.toy());base,f,g,protocol,release,manifest,c=args
  f['histories'][0]['sessions'][0]['events'][0]['text']='é';g['questions']['toy/q']['forbidden_inferences']=[]
  from author_counts import derive_resources
  resources,rows=derive_resources(f,g);manifest['counts']=resources;manifest['per_history']=rows;release['counts']=resources
  (base/'formal-release.json').write_text(json.dumps(release));(base/'holdout/fixture.json').write_text(json.dumps(f));(base/'holdout/gold.json').write_text(json.dumps(g))
  parent=json.loads((base/'release-verification.json').read_bytes());parent['counts']['max_history_sessions']=1;parent['formal_release_sha256']=sha((base/'formal-release.json').read_bytes());(base/'release-verification.json').write_text(json.dumps(parent))
  c.update(author_count_contract='r23-resource-counts-v1',release_verification_sha256=sha((base/'release-verification.json').read_bytes()),fixture_sha256=sha((base/'holdout/fixture.json').read_bytes()),gold_sha256=sha((base/'holdout/gold.json').read_bytes()))
  return args
 def test_selected_r23_complete_count_consumer(self):
  checks=[];out=validate_counts(*self.selected(),lambda v,k:checks.append(v));self.assertTrue(all(checks));self.assertEqual(out['counts']['max_history_sessions'],1)
 def test_selected_null_rejected(self):
  args=self.selected();args[-1]['author_count_contract']=None
  with self.assertRaisesRegex(ValueError,'author_count_contract_selected'):validate_counts(*args,lambda v,k:None)
 def test_wrong_declared_event_length_rejected(self):
  args=self.selected();args[-2]['counts']['maximum_event_characters']=2
  with self.assertRaisesRegex(ValueError,'independent_complete_author_resource_counts'):validate_counts(*args,lambda v,k:None)
 def test_wrong_validator_pin_fails(self):
  args=list(self.toy());args[-1]['validator_sha256']='0'*64
  with self.assertRaisesRegex(ValueError,'count_validator_pin'):validate_counts(*args,lambda v,k:None)
 def test_stale_parent_pin_fails(self):
  args=list(self.toy());args[-1]['release_verification_sha256']='0'*64
  with self.assertRaisesRegex(ValueError,'count_parent_evidence_pin'):validate_counts(*args,lambda v,k:None)
if __name__=='__main__':unittest.main()
