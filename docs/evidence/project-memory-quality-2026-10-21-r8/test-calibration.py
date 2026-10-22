"""Offline checks for the public oracle and its separation from model inputs."""
import importlib.util
import json
from pathlib import Path
import unittest


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('r8_public_controls', HERE / 'public-calibration-library.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PublicOracleTests(unittest.TestCase):
    def setUp(self):
        self.controls = json.loads((HERE / 'judge-contract-controls.json').read_text())

    def test_only_reviewed_nonempty_sets_match(self):
        expected = self.controls['controls'][4]['expected']
        for labels in (['false_merge'], ['project_mixing'], ['false_merge', 'project_mixing']):
            self.assertTrue(module.expectation_matches({'fact_recall': [True, False], 'critical_errors': labels}, expected))
        for labels in ([], ['false_closure'], ['deleted_resurrection']):
            self.assertFalse(module.expectation_matches({'fact_recall': [True, False], 'critical_errors': labels}, expected))

    def test_unreviewed_oracle_rejected(self):
        module.validate_oracle(self.controls)
        self.controls['controls'][4]['expected']['critical_error_alternative_sets'].append([])
        with self.assertRaises(ValueError):
            module.validate_oracle(self.controls)

    def test_new_expectations_do_not_enter_payload(self):
        helper = module.load_helper(module.DEFAULT_HELPER)
        original = json.loads((HERE.parent / 'project-memory-quality-2026-10-21-r3/judge-contract-controls.json').read_text())
        self.assertEqual(module.prepare_packets(helper, self.controls)[-1][1],
                         module.prepare_packets(helper, original)[-1][1])

    def test_grouped_controls_keep_case_visibility_separate(self):
        helper = module.load_helper(module.DEFAULT_HELPER)
        payload = module.prepare_packets(helper, self.controls)[-1][1]
        self.assertNotIn('shared_reader_visible_memory', payload)
        self.assertNotIn('judge_source_catalog', payload)
        self.assertEqual(set(payload['case_source_catalogs']),
                         {case['id'] for case in payload['cases']})
        for case in payload['cases']:
            self.assertEqual(len(case['reader_visible_memory']), 1)
            self.assertLessEqual(len(case['reader_visible_memory'][0]), 6000)
            own = payload['case_source_catalogs'][case['id']]
            own_ids = {source['source_id'] for source in own}
            self.assertTrue(all(cite['source_id'] in own_ids
                                for cite in case['answer']['citations']))
            for other in payload['cases']:
                if other['id'] != case['id']:
                    other_ids = {source['source_id'] for source in
                                 payload['case_source_catalogs'][other['id']]}
                    self.assertTrue(own_ids.isdisjoint(other_ids))


if __name__ == '__main__':
    unittest.main()
