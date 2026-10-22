"""Public toy explicit R23 event-length/count contract; no actual cases."""
import unittest
from author_counts import derive_resources
class ResourceDerivation(unittest.TestCase):
 def fixture(self):
  f={'histories':[{'id':'toy','questions':[{'id':'q'}],'sessions':[{'events':[{'type':'user','text':'é','tool_capture':{'payload':{'raw':'x'*1000}}},{'type':'delete_observation','text':'longest deletion text'}],'checkpoints':[]}]}]};g={'questions':{'toy/q':{'required_facts':[{'support':[{}]}],'forbidden_inferences':[{'error_type':'deleted_resurrection'}]}}};return f,g
 def test_all_events_python_characters_nested_excluded(self):
  f,g=self.fixture();r,rows=derive_resources(f,g);self.assertEqual(r['maximum_event_characters'],len('longest deletion text'));self.assertEqual(r['deletion_controls'],1);self.assertEqual(r['observations'],1);self.assertEqual(r['typed_negative_counts']['deleted_resurrection'],1)
 def test_python_characters_not_utf8_bytes(self):
  f,g=self.fixture();f['histories'][0]['sessions'][0]['events'][1]['text']='';r,rows=derive_resources(f,g);self.assertEqual(r['maximum_event_characters'],1)
 def test_missing_text_fails_closed(self):
  f,g=self.fixture();del f['histories'][0]['sessions'][0]['events'][1]['text']
  with self.assertRaises(ValueError):derive_resources(f,g)
if __name__=='__main__':unittest.main()
