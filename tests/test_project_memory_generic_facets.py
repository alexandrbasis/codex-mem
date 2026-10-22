"""Subject-free read facets use temporary fictional project ledgers."""
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree

from codex_mem.config import configure
from codex_mem.project_memory import ProjectMemory
from codex_mem.project_memory_retrieval import build_project_brief
from codex_mem.store import Store


class GenericProjectFacetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / 'observatory'
        self.project.mkdir()
        self.store = Store(Path(temporary.name) / 'data')
        self.addCleanup(self.store.close)
        configure(self.store.data_dir, project_memory_mode='pilot',
                  project_memory_projects=[str(self.project)], capture_scope='all')

    def add_episode(self, text, *, outcome='', session='first', target=None, context=''):
        source = self.store.remember(self.project, 'Observatory task', text + context,
            source='hook:UserPromptSubmit', session_id=session)
        tool = self.store.remember(self.project, 'Checker output', outcome,
            source='hook:PostToolUse', session_id=session) if outcome else None
        memory = ProjectMemory(self.store)
        snapshot = memory.claim(self.project)
        def ref(entry, quote):
            return {'source_id': entry['id'], 'start': 0, 'end': len(quote), 'quote': quote}
        evidence = {'summary': [ref(source, text)], 'goal': [ref(source, text)]}
        item = {'key': 'task', 'summary': text, 'goal': text, 'evidence': evidence}
        if outcome:
            item['outcome'] = outcome
            evidence['outcome'] = [ref(tool, outcome)]
        if target:
            item['continuation_of'] = target
            evidence['continuation_of'] = [ref(source, text)]
        result = memory.commit(snapshot, {'episodes': [item], 'claims': [], 'relations': []})
        self.assertEqual(result['status'], 'committed', result)
        return result['object_ids'][0]

    def test_generic_delivery_question_recalls_checker_acceptance_without_word_overlap(self):
        outcome = 'The spectral checker exited with code zero. The assembled package passed acceptance.'
        self.add_episode('OBS-42 builds the atlas package.', outcome=outcome)
        brief = build_project_brief(self.store, self.project, query='Which results are confirmed?')
        self.assertTrue(brief['episodes'], brief['xml'])
        tree = ElementTree.fromstring(brief['xml'])
        self.assertIn(outcome, ''.join(tree.itertext()))
        self.assertIsNotNone(tree.find("./item/field/source[@source_role='tool_record']"))
        self.assertLessEqual(len(brief['xml']), 6000)
        for query in ('Which telescope results are confirmed?', 'How does the coating behave?'):
            with self.subTest(query=query):
                unknown = build_project_brief(self.store, self.project, query=query)
                self.assertEqual(unknown['records'], [])


    def test_generic_facets_keep_exact_versions_session_exclusion_and_deletion(self):
        wanted = self.add_episode('OBS-42 package uses version 2.8.4.',
                                  outcome='Acceptance checks passed for version 2.8.4.', session='wanted')
        near = self.add_episode('OBS-42 package uses version 2.8.40.',
                                outcome='Acceptance checks passed for version 2.8.40.', session='near')
        scoped = build_project_brief(self.store, self.project,
            query='Which results are confirmed for OBS-42 version 2.8.4?')
        self.assertEqual([wanted], [item['id'] for item in scoped['episodes']])
        excluded = build_project_brief(self.store, self.project,
            query='Which results are confirmed?', exclude_session_id='wanted')
        self.assertEqual([near], [item['id'] for item in excluded['episodes']])
        self.assertNotIn('version 2.8.4.', excluded['xml'])
        ProjectMemory(self.store).forget(self.project, [wanted])
        deleted = build_project_brief(self.store, self.project, query='Which results are confirmed?')
        self.assertNotIn(wanted, [item['id'] for item in deleted['episodes']])
        self.assertNotIn('Acceptance checks passed for version 2.8.4.', deleted['xml'])

    def test_latest_request_relation_includes_history_and_separate_task_source_context(self):
        first = self.add_episode('OBS-42 builds the atlas package.', session='original')
        current = self.add_episode('Continue OBS-42: inspect its revised package.',
                                   session='revision', target=first)
        separate = self.add_episode('OBS-89 checks the telescope registry.', session='registry',
            context=' This is a separate task, not a continuation of the atlas package.')
        state = build_project_brief(self.store, self.project, query='What has been delivered?')
        self.assertNotIn(first, [item['id'] for item in state['episodes']])
        brief = build_project_brief(self.store, self.project,
                                   query='How is the latest instruction connected to earlier tasks?')
        self.assertEqual('continuity', brief['projection'])
        self.assertEqual({first, current, separate}, {item['id'] for item in brief['episodes']})
        tree = ElementTree.fromstring(brief['xml'])
        standalone = tree.find("./item[@id='" + separate + "']")
        self.assertIsNotNone(standalone, brief['xml'])
        self.assertEqual(standalone.get('continuity_basis'), 'standalone')
        self.assertNotIn('parent_episode_id', standalone.attrib)
        boundary = standalone.find("./field[@name='source_context']/source")
        self.assertIsNotNone(boundary, brief['xml'])
        self.assertIn('separate task', boundary.findtext('quote_excerpt'))
        self.assertEqual(boundary.get('source_role'), 'user_intent')
        self.assertLessEqual(len(brief['xml']), 6000)
        excluded = build_project_brief(self.store, self.project,
            query='How does the last request relate to prior work?', exclude_session_id='registry')
        self.assertNotIn(separate, [item['id'] for item in excluded['episodes']])
        self.assertNotIn('telescope registry', excluded['xml'])

    def test_current_assessment_keeps_superseded_episode_with_scope_guards(self):
        prior = self.add_episode('OBS-42 builds the atlas package.',
            outcome='The earlier checker reported the atlas package complete.', session='original')
        current = self.add_episode('Continue OBS-42: verify its revised package.',
            outcome='The current checker found an incomplete atlas index.',
            session='revision', target=prior)
        other = self.add_episode('OBS-89 checks the telescope registry.', session='registry')

        assessment = build_project_brief(self.store, self.project,
            query='Was the earlier assistant completion claim accurate?')
        self.assertTrue({prior, current}.issubset({item['id'] for item in assessment['episodes']}))
        scoped = build_project_brief(self.store, self.project,
            query='Was the earlier assistant completion claim for OBS-42 accurate?')
        self.assertEqual({prior, current}, {item['id'] for item in scoped['episodes']})
        self.assertNotIn(other, [item['id'] for item in scoped['episodes']])
        self.assertLessEqual(len(scoped['xml']), 6000)
        ElementTree.fromstring(scoped['xml'])

        state = build_project_brief(self.store, self.project,
            query='Which results are confirmed for OBS-42?')
        self.assertEqual([current], [item['id'] for item in state['episodes']])
        historical = build_project_brief(self.store, self.project,
            query='What did the assistant report for OBS-42 say as of 2031-02-03?')
        self.assertEqual([], historical['records'])
        excluded = build_project_brief(self.store, self.project,
            query='Was the earlier assistant completion claim for OBS-42 accurate?',
            exclude_session_id='original')
        self.assertNotIn(prior, [item['id'] for item in excluded['episodes']])
        self.assertNotIn('earlier checker reported', excluded['xml'])



if __name__ == '__main__':
    unittest.main()
