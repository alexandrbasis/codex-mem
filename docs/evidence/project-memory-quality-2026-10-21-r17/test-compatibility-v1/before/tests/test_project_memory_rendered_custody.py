"""Original source fallback depends on displayed evidence, not candidate coverage."""
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree

from codex_mem.config import configure
from codex_mem.project_memory import ProjectMemory
from codex_mem.project_memory_retrieval import build_project_brief
from codex_mem.store import Store


class RenderedCustodyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / 'exhibit'
        self.project.mkdir()
        self.store = Store(Path(temporary.name) / 'data')
        self.addCleanup(self.store.close)
        configure(self.store.data_dir, capture_scope='all', project_memory_mode='pilot',
                  project_memory_projects=[str(self.project)])

    def populate(self, claim_count):
        goal = 'ARC-51 prepares the exhibit reading cards.'
        reason = ' Keep the cards in grayscale because the gallery printer uses one ink cartridge.'
        source = self.store.remember(self.project, 'ARC-51 reading cards', goal + reason,
                                     source='hook:UserPromptSubmit', session_id='cards')
        claims = []
        for index in range(claim_count):
            text = f'ARC-51 approval item {index}: ' + 'keep this separate review pending ' * 1 + '.'
            row = self.store.remember(self.project, 'ARC-51 approval', text,
                                     source='hook:UserPromptSubmit', session_id='cards')
            ref = {'source_id': row['id'], 'start': 0, 'end': len(text), 'quote': text}
            claims.append({'key': f'approval-{index}', 'kind': 'requirement', 'workflow_state': 'open',
                           'text': text, 'scope': {'environment': '', 'version': ''},
                           'evidence': {'text': [ref], 'workflow_state': [ref]}})
        memory = ProjectMemory(self.store)
        snapshot = memory.claim(self.project)
        episode = {'key': 'cards', 'summary': goal, 'goal': goal, 'rationale': reason,
            'evidence': {'summary': [{'source_id': source['id'], 'start': 0, 'end': len(goal), 'quote': goal}],
                         'goal': [{'source_id': source['id'], 'start': 0, 'end': len(goal), 'quote': goal}],
                         'rationale': [{'source_id': source['id'], 'start': len(goal), 'end': len(goal + reason), 'quote': reason}]}}
        self.assertEqual(memory.commit(snapshot, {'episodes': [episode], 'claims': claims, 'relations': []})['status'], 'committed')
        return source, goal, reason

    def test_candidate_goal_and_reason_cannot_suppress_unrendered_source_fallback(self):
        source, goal, reason = self.populate(8)
        brief = build_project_brief(self.store, self.project, query='Why ARC-51?', budget=6000)
        self.assertTrue(any('grayscale' in str(field) for record in brief['episodes'] for field in record['fields']))
        tree = ElementTree.fromstring(brief['xml'])
        self.assertIn(reason.strip(), ''.join(tree.itertext()), brief['xml'])
        quoted = [ptr.findtext('quote_excerpt') for ptr in tree.findall('.//source')
                  if ptr.get('source_id') == source['id'] and ptr.findtext('quote_excerpt')]
        self.assertTrue(any(reason.strip() in text for text in quoted))
        self.assertLessEqual(len(brief['xml']), 6000)
        self.assertGreater(brief['render_omissions']['fields'], 0)

    def test_fallback_obeys_session_exclusion_and_source_deletion(self):
        source, goal, reason = self.populate(8)
        hidden = build_project_brief(self.store, self.project, query='Why ARC-51?',
                                     exclude_session_id='cards', budget=6000)
        self.assertNotIn(reason.strip(), hidden['xml'])
        self.store.forget(self.project, [source['id']])
        deleted = build_project_brief(self.store, self.project, query='Why ARC-51?', budget=6000)
        self.assertNotIn(reason.strip(), deleted['xml'])
        self.assertFalse(any(source['id'] in record.get('source_ids', []) for record in deleted['intent_custody']))

    def test_fully_rendered_source_is_not_quoted_again_as_custody(self):
        source, goal, reason = self.populate(0)
        brief = build_project_brief(self.store, self.project, query='Why ARC-51?', budget=6000)
        tree = ElementTree.fromstring(brief['xml'])
        self.assertIn(reason.strip(), ''.join(tree.itertext()))
        self.assertIsNone(tree.find("./item[@lane='intent_custody']"), brief['xml'])
        self.assertEqual(''.join(tree.itertext()).count(reason.strip()), 1)
        self.assertTrue(brief['intent_custody'])


if __name__ == '__main__':
    unittest.main()
