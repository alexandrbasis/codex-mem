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
        self.assertFalse(p.continuation)
    def test_genuine_history_remains_protected(self):
        for q in ['What earlier telescope results exist?', 'What originally governed the telescope task?', 'How much of the earlier telescope request was withdrawn as of version 2.4?', 'Does the later instruction belong to the earlier telescope task at the time?', 'How much of the earlier telescope request was withdrawn in 2024?', 'Does the later instruction belong to the earlier telescope task as of version 2.4?', 'Что было решено раньше?', 'По состоянию на вчера что было решено?']:
            with self.subTest(q=q):self.assertTrue(plan_query(q,self.project).historical)
    def test_literals_and_named_subjects_stay_hard_or_topical(self):
        p=plan_query('How much of the earlier `packet_rule` request was withdrawn?',self.project)
        self.assertIn('packet_rule',p.identifiers)
        self.assertFalse(p.matches({'text':'unrelated other_rule'}))
        p=plan_query('How much of the earlier spectrometer request was withdrawn?',self.project)
        self.assertEqual('spectrometer',p.answer_subject)
        scoped=plan_query('How much of the earlier TASK-271 telescope version 2.4 local request was withdrawn?',self.project)
        self.assertEqual(('task-271',),scoped.task_ids)
        self.assertEqual(('2.4',),scoped.versions)
        self.assertEqual(('local',),scoped.environments)
        self.assertFalse(scoped.matches({'text':'TASK-272 telescope 2.4'}))
        self.assertFalse(scoped.matches({'text':'TASK-271 telescope 2.5'}))
    def test_public_store_keeps_topicless_relation_proof_attributed(self):
        import tempfile
        from codex_mem.config import configure
        from codex_mem.store import Store
        from codex_mem.project_memory import ProjectMemory
        from codex_mem.project_memory_retrieval import build_project_brief
        from xml.etree import ElementTree as ET
        for later in ('This continues the same task. Drop the diagram.',
                      'This is a separate task. Drop the diagram.'):
            with self.subTest(later=later), tempfile.TemporaryDirectory() as directory:
                project=Path(directory)/'public-proof'
                project.mkdir()
                with Store(Path(directory)/'data') as store:
                    configure(store.data_dir,capture_scope='all',project_memory_mode='pilot',
                              project_memory_projects=[str(project)])
                    store.remember(project,'Initial work','Telescope work remains open.',
                                   source='hook:UserPromptSubmit',session_id='initial')
                    row=store.remember(project,'Later instruction',later,
                                       source='hook:UserPromptSubmit',session_id='later')
                    memory=ProjectMemory(store)
                    while (job := memory.claim(project)) is not None:
                        memory.commit(job,{'episodes':[],'claims':[],'relations':[]})
                    question='Does the later instruction belong to the earlier telescope task?'
                    for xml in (build_project_brief(store,project,query=question)['xml'],
                                store.context(project,query=question,budget=6000,allow_remote=False,include_health=False)):
                        self.assertIn(later,xml)
                        root=ET.fromstring(xml)
                        containers=[item for item in root.iter('item') if any(
                            source.get('source_id')==row['id'] for source in item.iter('source'))]
                        self.assertTrue(containers)
                        self.assertTrue(all(item.get('relationship_to_query')=='unresolved' for item in containers))
                        self.assertFalse(list(root.iter('relation')))
                    hidden=build_project_brief(store,project,query=question,exclude_session_id='later')['xml']
                    self.assertNotIn(row['id'],hidden)
                    store.forget(project,[row['id']])
                    self.assertNotIn(row['id'],build_project_brief(store,project,query=question)['xml'])

if __name__=='__main__':unittest.main()
