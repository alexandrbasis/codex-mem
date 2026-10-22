"""Public literal-transport tests; no renderer, retrieval or trial input calls."""
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('assessor',Path(__file__).with_name('assess-renderer.py'))
a=importlib.util.module_from_spec(spec);spec.loader.exec_module(a)

class RendererAssessorTests(unittest.TestCase):
    def setUp(self):
        self.originals={'toy':{'source_role':'tool_record','fields':{'body':'ABCDEF','tool_response.output':'ABCDEF'}}}
        self.span={'source_id':'toy','source_field':'body','source_role':'tool_record','start':1,'end':5,'quote':'BCDE','tool_use_id':'tool-one'}
    def test_full_exact_field_quote_covers_old_partial(self):
        full={**self.span,'start':0,'end':6,'quote':'ABCDEF'}
        self.assertEqual([],a.coverage_difference([self.span],[full],self.originals))
        self.assertEqual([(0,1),(5,6)],[(r['start'],r['end'])for r in a.coverage_difference([full],[self.span],self.originals)])
    def test_same_source_different_field_is_a_real_transport_loss(self):
        other={**self.span,'source_field':'tool_response.output'}
        self.assertEqual('BCDE',a.coverage_difference([self.span],[other],self.originals)[0]['quote'])
    def test_role_and_tool_identity_are_not_silently_substituted(self):
        for update in ({'source_role':'assistant_report'},{'tool_use_id':'tool-two'}):
            with self.subTest(update=update):
                self.assertEqual(1,len(a.coverage_difference([self.span],[{**self.span,**update}],self.originals)))
    def test_contiguous_intervals_cover_but_one_character_gap_does_not(self):
        self.assertEqual([],a.subtract([(0,6)],[(0,3),(3,6)]))
        self.assertEqual([[2,3]],a.subtract([(0,6)],[(0,2),(3,6)]))
    def test_wrong_literal_role_or_offsets_rejected(self):
        a.validate_catalog([self.span],self.originals)
        for update in ({'quote':'BCDX'},{'start':0},{'source_role':'user_intent'}):
            with self.subTest(update=update),self.assertRaises(ValueError):
                a.validate_catalog([{**self.span,**update}],self.originals)
    def test_xml_escape_transport_keeps_original_quote(self):
        helper=a.load_module('toy_frozen_catalog',Path(__file__).resolve().parents[1]/'project-memory-quality-2026-10-21-r16/frozen-helper.py')
        body='Fish & chips < warm > cold.'
        original={'s':{'source_role':'user_intent','fields':{'body':body}}}
        escaped='Fish &amp; chips &lt; warm &gt; cold.'
        xml='<project-memory><item><source source_id="s" source_field="body" source_role="user_intent" start="0" end="27"><quote_excerpt>'+escaped+'</quote_excerpt></source></item></project-memory>'
        catalog=helper.reader_citation_catalog([{'id':'toy','memory':xml}],original)
        self.assertEqual(body,catalog[0]['quote'])
        a.validate_catalog(catalog,original)
        self.assertEqual(len(escaped),a.xml_metrics(xml)['escaped_quote_text_chars'])
    def test_invalid_interval_order_and_empty_range_rejected(self):
        for interval in [(2,1),(1,1),(-1,2)]:
            with self.subTest(interval=interval),self.assertRaises(ValueError):a.merge([interval])

if __name__=='__main__':unittest.main()
