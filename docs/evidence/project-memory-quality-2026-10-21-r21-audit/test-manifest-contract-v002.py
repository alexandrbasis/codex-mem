#!/usr/bin/env python3
"""Only public manifest/generic-pack metadata; never private archive or cases."""
import hashlib,json,tempfile,unittest
from pathlib import Path
from release_schema_v002 import validate_generic_manifest_pins
B=Path(__file__).resolve().parent
class SelectedManifestContract(unittest.TestCase):
 def fixture(self):
  tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);base=Path(tmp.name);(base/'contract.json').write_bytes(b'public schema');pins={'contract.json':hashlib.sha256(b'public schema').hexdigest()};return base,pins
 def test_both_explicit_schemas_positive(self):
  for contract,field in [('generic-pack-files-v1','generic_pack_files'),('generic-contract-pins-v1','generic_contract_pins')]:
   base,pins=self.fixture();checks=[];result=validate_generic_manifest_pins(base,{field:pins},contract,lambda v,k:checks.append(v));self.assertEqual(result,pins);self.assertEqual(checks,[True])
 def test_wrong_selected_field_has_no_fallback(self):
  base,pins=self.fixture()
  for contract,other in [('generic-pack-files-v1','generic_contract_pins'),('generic-contract-pins-v1','generic_pack_files')]:
   with self.assertRaises(KeyError):validate_generic_manifest_pins(base,{other:pins},contract,lambda v,k:None)
 def test_missing_or_unknown_selection_rejected(self):
  base,pins=self.fixture()
  for contract in (None,'unknown',True):
   with self.assertRaises(ValueError):validate_generic_manifest_pins(base,{'generic_pack_files':pins},contract,lambda v,k:None)
 def test_changed_public_file_fails_check(self):
  base,pins=self.fixture();(base/'contract.json').write_bytes(b'changed');checks=[];validate_generic_manifest_pins(base,{'generic_pack_files':pins},'generic-pack-files-v1',lambda v,k:checks.append(v));self.assertEqual(checks,[False])
 def test_nonbasename_schema_rejected(self):
  base,pins=self.fixture()
  with self.assertRaises(ValueError):validate_generic_manifest_pins(base,{'generic_pack_files':{'../contract.json':next(iter(pins.values()))}},'generic-pack-files-v1',lambda v,k:None)
 def test_actual_contents_free_r21_metadata_and_five_public_pins(self):
  base=B.parent/'project-memory-quality-2026-10-21-r21';manifest=json.loads((base/'sealed-author-manifest.json').read_bytes());checks=[];pins=validate_generic_manifest_pins(base,manifest,'generic-pack-files-v1',lambda v,k:checks.append(v));self.assertEqual(len(pins),5);self.assertTrue(all(checks));self.assertIn('generic-pack-pins.json',pins)
if __name__=='__main__':unittest.main()
