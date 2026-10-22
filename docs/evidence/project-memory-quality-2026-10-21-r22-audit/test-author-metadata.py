"""Actual contents-free metadata shape only; no private files or hash targets."""
import copy,hashlib,json,unittest
from pathlib import Path
from author_counts import validate_resource_shape
P=Path(__file__).resolve().parent.parent/'project-memory-quality-2026-10-21-r22-preparation/author-manifest.v002.json'
class AuthorMetadata(unittest.TestCase):
 def metadata(self):
  self.assertEqual(hashlib.sha256(P.read_bytes()).hexdigest(),'c2604b0705cf5108fcec490b842c3a384df29e29cf23a6ea4df2a3d7521c61f2');return json.loads(P.read_bytes())
 def test_actual_contents_free_shape(self):
  m=self.metadata();self.assertNotIn('root',m);self.assertNotIn('formal_release',m);self.assertEqual(validate_resource_shape(m['counts']),m['counts'])
 def test_missing_field_rejected(self):
  c=self.metadata()['counts'];del c['maximum_event_characters']
  with self.assertRaises(ValueError):validate_resource_shape(c)
 def test_perhistory_field_or_aggregate_change_rejected(self):
  c=self.metadata()['counts'];c['per_history'][0]['required_facts']+=1
  with self.assertRaises(ValueError):validate_resource_shape(c)
 def test_negative_map_change_rejected(self):
  c=self.metadata()['counts'];c['typed_negative_counts']['false_merge']+=1
  with self.assertRaises(ValueError):validate_resource_shape(c)
if __name__=='__main__':unittest.main()
