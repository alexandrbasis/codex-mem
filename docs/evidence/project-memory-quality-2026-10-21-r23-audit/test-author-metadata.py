"""Actual contents-free metadata shape only; no private files or hash targets."""
import copy,hashlib,json,unittest
from pathlib import Path
from author_counts import validate_resource_shape
P=Path(__file__).resolve().parent.parent/'project-memory-quality-2026-10-21-r23-preparation/author-manifest.v002.json'
class AuthorMetadata(unittest.TestCase):
 def metadata(self):
  self.assertEqual(hashlib.sha256(P.read_bytes()).hexdigest(),'7a5b4a4d6dc48d78b4f1e8695385ac89c81849871b873f0aeb782096e6bc219c');return json.loads(P.read_bytes())
 def test_actual_contents_free_shape(self):
  m=self.metadata();self.assertNotIn('root',m);self.assertNotIn('formal_release',m);self.assertEqual(validate_resource_shape(m['counts'],m['per_history']),m['counts'])
 def test_nested_rows_or_extra_keys_rejected(self):
  m=self.metadata();c=m['counts'];c['per_history']=m['per_history']
  with self.assertRaises(ValueError):validate_resource_shape(c,m['per_history'])
 def test_missing_top_rows_rejected(self):
  m=self.metadata()
  with self.assertRaises(TypeError):validate_resource_shape(m['counts'])
 def test_missing_field_rejected(self):
  m=self.metadata();c=m['counts'];del c['maximum_event_characters']
  with self.assertRaises(ValueError):validate_resource_shape(c,m['per_history'])
 def test_perhistory_field_or_aggregate_change_rejected(self):
  m=self.metadata();c=m['counts'];m['per_history'][0]['required_facts']+=1
  with self.assertRaises(ValueError):validate_resource_shape(c,m['per_history'])
 def test_negative_map_change_rejected(self):
  m=self.metadata();c=m['counts'];c['typed_negative_counts']['false_merge']+=1
  with self.assertRaises(ValueError):validate_resource_shape(c,m['per_history'])
if __name__=='__main__':unittest.main()
