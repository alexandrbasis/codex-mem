"""Prospective public contract checks. Native fidelity is not measured here."""
import hashlib,importlib.util,json
from pathlib import Path
import unittest
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
def load(name,path):
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
probe=load('r23_public_pack_probe',HERE/'prepare-reader-probe.py')
helper=load('r23_public_pack_helper',ROOT/'scripts/project_memory_consolidation_experiment.py')

class PublicFidelityPackTests(unittest.TestCase):
    def test_eight_reader_requests_require_ten_public_calls_with_visible_support(self):
        corpus=helper.read(HERE/'public-reader-regressions.json')
        protocol=probe.default_protocol(corpus)
        prepared=probe.prepare(helper,protocol,corpus)
        self.assertEqual(len(prepared['reader_requests']),8)
        self.assertEqual(corpus['planned_native_calls']['total'],10)
        self.assertEqual(corpus['planned_native_calls']['fresh_probe_judge_batch'],1)
        self.assertEqual(corpus['planned_native_calls']['fresh_known_judge_control'],1)
        self.assertEqual(prepared['reader_prompt_version'],'role-attribution-per-question-assertions-fidelity-v1')
        for request,reference in zip(prepared['reader_requests'],prepared['evaluator_references_not_reader_input']):
            self.assertTrue(reference['required_fact_visibility'])
            self.assertTrue(all(x['reader_visible'] for x in reference['required_fact_visibility']))
            self.assertLessEqual(reference['context']['context_chars'],6000)
            self.assertEqual(request['request']['model'],'gpt-6.1-sol')
            self.assertIn('Retain explicit quantifiers',request['request']['prompt'])
    def test_carried_six_cases_calibration_and_four_author_contracts_unchanged(self):
        corpus=helper.read(HERE/'public-reader-regressions.json');legacy=helper.read(HERE/'legacy-public-reader-regressions.json')
        self.assertEqual(corpus['cases'][:6],legacy['cases'])
        self.assertEqual(len({c['id'] for c in corpus['cases']}),8)
        old=HERE.with_name(HERE.name[:-2]+'20')
        for name in ('judge-contract-controls.json','public-calibration-library.py'):
            self.assertEqual((HERE/name).read_bytes(),(old/name).read_bytes())
        pins=helper.read(ROOT/'docs/evidence/project-memory-quality-2026-10-21-r21-preparation/author-pack-preparation.json')['files']
        for name,pin in pins.items():self.assertEqual(hashlib.sha256((HERE/name).read_bytes()).hexdigest(),pin)

if __name__=='__main__':unittest.main()
