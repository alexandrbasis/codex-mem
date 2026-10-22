"""Proposed public planner regression slices; run red only after archive approval."""
import unittest
from pathlib import Path
from codex_mem.project_memory_query import plan_query
class CurrentReferenceTests(unittest.TestCase):
    project=Path('/tmp/public-request-reference')
    def test_current_withdrawal_retains_residual_subject(self):
        p=plan_query('How much of the earlier telescope request was withdrawn?',self.project)
        self.assertFalse(p.historical)
        self.assertEqual('telescope',p.answer_subject)
        self.assertIn('current_revision',p.answer_facets)
    def test_current_relationship_retains_task_subject(self):
        p=plan_query('Does the later instruction belong to the earlier telescope task?',self.project)
        self.assertFalse(p.historical)
        self.assertEqual('telescope',p.answer_subject)
        self.assertEqual('continuity',p.projection)
    def test_genuine_history_remains_protected(self):
        for q in ['What earlier telescope results exist?', 'What originally governed the telescope task?', 'How much of the earlier telescope request was withdrawn as of version 2.4?', 'Does the later instruction belong to the earlier telescope task at the time?', 'Что было решено раньше?', 'По состоянию на вчера что было решено?']:
            with self.subTest(q=q):self.assertTrue(plan_query(q,self.project).historical)
    def test_literals_and_named_subjects_stay_hard_or_topical(self):
        p=plan_query('How much of the earlier `packet_rule` request was withdrawn?',self.project)
        self.assertIn('packet_rule',p.identifiers)
        self.assertFalse(p.matches({'text':'unrelated other_rule'}))
        p=plan_query('How much of the earlier spectrometer request was withdrawn?',self.project)
        self.assertEqual('spectrometer',p.answer_subject)
if __name__=='__main__':unittest.main()
