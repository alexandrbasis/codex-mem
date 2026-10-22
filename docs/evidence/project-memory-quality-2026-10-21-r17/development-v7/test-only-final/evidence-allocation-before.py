"""Whole execution evidence competes fairly with compact original user sources."""
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree

from codex_mem.config import configure
from codex_mem.project_memory import ProjectMemory
from codex_mem.project_memory_retrieval import build_project_brief, render_project_brief
from codex_mem.store import Store
from codex_mem.tool_io import normalize_capture


class EvidenceAllocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / 'exhibit'
        self.project.mkdir()
        self.store = Store(Path(temporary.name) / 'data')
        self.addCleanup(self.store.close)
        configure(self.store.data_dir, capture_scope='all', project_memory_mode='pilot',
                  project_memory_projects=[str(self.project)])

    def populate(self):
        def ref(row, quote):
            start = row['body'].index(quote)
            return {'source_id': row['id'], 'start': start, 'end': start + len(quote), 'quote': quote}
        def commit(episode, claims=()):
            memory = ProjectMemory(self.store)
            result = memory.commit(memory.claim(self.project),
                {'episodes': [episode], 'claims': list(claims), 'relations': []})
            self.assertEqual(result['status'], 'committed', result)
        def claim(key, row, text):
            return {'key': key, 'kind': 'requirement', 'workflow_state': 'open', 'text': text,
                'scope': {'environment': '', 'version': ''},
                'evidence': {'text': [ref(row, text)], 'workflow_state': [ref(row, text)],
                             'rationale': [ref(row, text)]}, 'rationale': text}
        goal = 'The exhibit cards require grayscale because the printer uses one ink cartridge.'
        scope = ' Include paper cards and a digital preview.'
        user = self.store.remember(self.project, 'Exhibit cards', goal + scope,
            source='hook:UserPromptSubmit', session_id='request')
        assistant = self.store.remember(self.project, 'Draft report', 'I drafted both card views.',
            source='hook:Stop', session_id='request')
        commit({'key': 'draft', 'summary': goal + scope, 'goal': goal + scope, 'rationale': goal,
            'outcome': assistant['body'], 'evidence': {'summary': [ref(user, goal), ref(user, scope)],
                'goal': [ref(user, goal), ref(user, scope)], 'rationale': [ref(user, goal)],
                'outcome': [ref(assistant, assistant['body'])]}},
            [claim('ink', user, goal), claim('formats', user, scope)])
        change = 'Drop only the digital preview.'
        pending = ' The paper cards still need a contrast check.'
        user2 = self.store.remember(self.project, 'Retained exhibit cards', change + pending,
            source='hook:UserPromptSubmit', session_id='inspection')
        saved, unchecked = 'Paper cards: saved.', ' Contrast check: not run.'
        output = saved + unchecked
        capture = normalize_capture({'tool_name': 'functions.exec_command', 'session_id': 'inspection',
            'tool_use_id': 'inspect-cards', 'tool_input': {'cmd': 'exhibit inspect --cards'},
            'tool_response': {'exit_code': 0, 'output': output}}, project=self.project)
        tool = self.store.remember(self.project, 'Cards inspection', output,
            source='hook:PostToolUse', session_id='inspection', tool_capture=capture)
        commit({'key': 'inspection', 'summary': change + pending + saved, 'goal': change + pending,
            'rationale': change, 'open_work': pending + unchecked, 'outcome': saved,
            'evidence': {'summary': [ref(user2, change), ref(user2, pending), ref(tool, saved)],
                'goal': [ref(user2, change), ref(user2, pending)], 'rationale': [ref(user2, change)],
                'open_work': [ref(user2, pending), ref(tool, unchecked)], 'outcome': [ref(tool, saved)]}})
        correction = 'Use case numbers, not room numbers, on the paper cards.'
        user3 = self.store.remember(self.project, 'Card numbering', correction,
            source='hook:UserPromptSubmit', session_id='correction')
        commit({'key': 'correction', 'summary': correction, 'goal': correction,
            'evidence': {'summary': [ref(user3, correction)], 'goal': [ref(user3, correction)]}},
            [claim('numbering', user3, correction)])
        return tool, goal, saved, unchecked

    def test_complete_execution_and_compact_reason_survive_competing_material(self):
        tool, reason, saved, unchecked = self.populate()
        brief = build_project_brief(self.store, self.project,
            query='What did the inspection confirm about the paper cards?', budget=6000)
        tree = ElementTree.fromstring(brief['xml'])
        text = ''.join(tree.itertext())
        for expected in (saved, unchecked.strip(), reason,
                         'Use case numbers, not room numbers, on the paper cards.'):
            self.assertIn(expected, text, brief['xml'])
        execution = tree.find("./item[@lane='source_evidence'][@source_role='tool_record']/execution")
        self.assertIsNotNone(execution, brief['xml'])
        self.assertEqual(execution.get('command'), 'exhibit inspect --cards')
        self.assertEqual(execution.get('exit_code'), '0')
        result = tree.find("./item[@lane='source_evidence'][@source_role='tool_record']/source[@source_field='tool_response.output']")
        self.assertIsNotNone(result, brief['xml'])
        self.assertEqual(result.findtext('quote_excerpt'), tool['body'])
        self.assertEqual(result.get('excerpt_status'), 'complete_capture_field')
        self.assertLessEqual(len(brief['xml']), 6000)
        self.assertGreater(brief['render_omissions']['fields'], 0)
        self.assertNotIn('workflow_state="completed"', brief['xml'])

    def test_execution_source_fences_survive_balancing(self):
        tool, _, saved, unchecked = self.populate()
        query = 'What did the inspection confirm about the paper cards?'
        excluded = build_project_brief(self.store, self.project, query=query,
            exclude_session_id='inspection', budget=6000)
        self.assertNotIn(saved, excluded['xml'])
        self.assertNotIn(unchecked.strip(), excluded['xml'])
        unknown = build_project_brief(self.store, self.project, query='What happened to OTHER-93?')
        self.assertEqual(unknown['records'], [])
        self.store.forget(self.project, [tool['id']])
        deleted = build_project_brief(self.store, self.project, query=query, budget=6000)
        self.assertNotIn(saved, deleted['xml'])
        self.assertNotIn(unchecked.strip(), deleted['xml'])

    def test_tiny_budget_omits_original_execution_group_together(self):
        self.populate()
        brief = build_project_brief(self.store, self.project,
            query='What did the inspection confirm about the paper cards?', budget=900)
        tree = ElementTree.fromstring(brief['xml'])
        self.assertIsNone(tree.find("./item[@lane='source_evidence'][@source_role='tool_record']"))
        self.assertGreater(brief['render_omissions']['records'], 0)
        self.assertIsNotNone(tree.find('omitted'))
        self.assertLessEqual(len(brief['xml']), 900)

    def execution_only_brief(self):
        tool, _, _, _ = self.populate()
        brief = build_project_brief(self.store, self.project,
            query='What did the inspection confirm about the paper cards?', budget=6000)
        record = next(record for record in brief['source_evidence'] if record['id'] == tool['id'])
        return {'records': [record], 'coverage': {}}

    def test_execution_group_fits_its_exact_serialized_boundary(self):
        brief = self.execution_only_brief()
        full = render_project_brief(brief, budget=6000)
        exact = render_project_brief(brief, budget=len(full))
        self.assertEqual(exact, full)
        item = ElementTree.fromstring(exact).find('item')
        self.assertEqual(len(item.findall('source')), 2)
        self.assertEqual(item.findall('field'), [])
        self.assertEqual(item.get('field_association'), 'original_execution_capture')
        self.assertNotIn('fields_omitted', item.attrib)

    def test_execution_group_is_omitted_one_character_below_boundary(self):
        brief = self.execution_only_brief()
        full = render_project_brief(brief, budget=6000)
        budget = len(full) - 1
        below = render_project_brief(brief, budget=budget)
        tree = ElementTree.fromstring(below)
        self.assertIsNone(tree.find('item'), below)
        self.assertEqual(tree.find('omitted').get('records'), '1')
        self.assertEqual(tree.find('omitted').get('fields'), '2')
        self.assertLessEqual(len(below), budget)


if __name__ == '__main__':
    unittest.main()
