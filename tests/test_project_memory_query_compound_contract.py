"""Natural compound modifiers keep topical words without requiring literal bytes."""
from pathlib import Path
import unittest

from codex_mem.project_memory_query import plan_query


class QueryCompoundContractTests(unittest.TestCase):
    project = Path('/public/compound-contract/workspace')

    def test_topical_compound_modifier_accepts_spaced_source_words(self):
        query = 'What did the assistant report about public-cut export after the complete caption review?'
        plan = plan_query(query, self.project)
        self.assertTrue(plan.matches({'summary':
            'The complete caption check passes. I have not exported the public cut yet.'}))
        self.assertNotIn('public-cut', plan.identifiers)
        self.assertEqual(query, plan.content_query)
        self.assertTrue(plan.topic_query)
        self.assertTrue(any('public' in value and 'cut' in value for value in plan.candidate_queries))

    def test_quoted_modifier_phrase_keeps_literal_identity(self):
        for quote in ('`', '"', "'"):
            query = f'What changed about {quote}amber-line export{quote}?'
            with self.subTest(quote=quote):
                plan = plan_query(query, self.project)
                self.assertFalse(plan.matches({'summary': 'The amber line export is pending.'}))
                self.assertIn('amber-line', plan.identifiers)


    def test_explicit_identity_labels_keep_compound_names_hard(self):
        for label in ('function', 'module', 'command', 'artifact', 'file', 'task',
                      'version', 'environment', 'env'):
            with self.subTest(label=label):
                plan = plan_query(f'What changed about {label} amber-line export?', self.project)
                self.assertFalse(plan.matches({'summary': 'The amber line export is pending.'}))
                self.assertIn('amber-line', plan.identifiers)


    def test_explicit_unknown_project_scope_keeps_name_hard_before_a_head(self):
        for label in ('project', 'projects'):
            plan = plan_query(f'What changed for {label} foreign-board coverage?', self.project)
            self.assertIn('foreign-board', plan.identifiers)
            self.assertFalse(plan.matches({'summary': 'The foreign board coverage is pending.'}))
        selected = self.project.parent / 'foreign-board'
        plan = plan_query('What changed for project foreign-board coverage?', selected)
        self.assertNotIn('foreign-board', plan.identifiers)
        self.assertIn('foreign-board', plan.scope_aliases)


    def test_call_after_compound_is_not_a_descriptive_head(self):
        plan = plan_query('What changed about bench-calibration inspect()?', self.project)
        self.assertIn('bench-calibration', plan.identifiers)
        self.assertFalse(plan.matches({'summary': 'The bench calibration inspection is pending.'}))


    def test_brief_preserves_source_bytes_spans_and_project_scope(self):
        import tempfile
        from xml.etree import ElementTree
        from codex_mem.config import configure
        from codex_mem.project_memory import ProjectMemory
        from codex_mem.project_memory_retrieval import build_project_brief
        from codex_mem.store import Store

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'workspace'
            neighbor = Path(directory) / 'neighbor'
            text = 'The complete caption check passes. I have not exported the public cut yet.'
            query = 'What did the assistant report about public-cut export after the complete caption review?'
            with Store(Path(directory) / 'data') as store:
                configure(store.data_dir, project_memory_mode='pilot', capture_scope='all',
                          project_memory_projects=[str(project), str(neighbor)])
                source = store.remember(project, 'Caption review report', text,
                                        source='hook:Stop', session_id='caption')
                foreign = store.remember(neighbor, 'Other report', text,
                                         source='hook:Stop', session_id='other')
                memory = ProjectMemory(store)
                ref = {'source_id': source['id'], 'start': 0, 'end': len(text), 'quote': text}
                memory.commit(memory.claim(project), {'episodes': [{
                    'key': 'caption', 'summary': text, 'outcome': text,
                    'evidence': {'summary': [ref], 'outcome': [ref]}}]})
                brief = build_project_brief(store, project, query=query, budget=6000)
                self.assertLessEqual(len(brief['xml']), 6000)
                sources = list(ElementTree.fromstring(brief['xml']).iter('source'))
                self.assertTrue(any(item.get('source_id') == source['id'] and
                    item.get('source_role') == 'assistant_report' and
                    item.get('source_field') == 'body' and item.get('start') == '0' and
                    item.get('end') == str(len(text)) and item.findtext('quote_excerpt') == text
                    for item in sources), brief['xml'])
                self.assertNotIn(foreign['id'], brief['xml'])
                self.assertEqual(text, source['body'])

    def test_bare_scope_and_coordinated_scope_names_keep_prior_guards(self):
        for query in ('What changed about foreign-board?',
                      'What changed about foreign-board after the review?',
                      'What changed for foreign-board and amber-line export?',
                      'What changed for foreign-board coverage and foreign-board?'):
            with self.subTest(query=query):
                plan = plan_query(query, self.project)
                self.assertIn('foreign-board', plan.identifiers)
                self.assertFalse(plan.matches({'summary': 'The foreign board coverage changed.'}))
        coordinated = plan_query('What changed for foreign-board and amber-line export?', self.project)
        self.assertIn('amber-line', coordinated.identifiers)

    def test_literal_shapes_and_other_hard_constraints_survive_descriptive_modifiers(self):
        for literal in ('`amber-line`', '"amber-line"', 'amber-line.py',
                        '/exports/amber-line', 'amber-line.member', 'amber-line()',
                        'amber_line', 'amberLine', 'JET-806'):
            with self.subTest(literal=literal):
                plan = plan_query(f'What changed about {literal} export?', self.project)
                self.assertFalse(plan.matches({'summary': 'The amber line export changed.'}))
        plan = plan_query('What changed about amber-line export for JET-806 version 4.6.2 in production?',
                          self.project)
        self.assertEqual(('jet-806',), plan.task_ids)
        self.assertEqual(('4.6.2',), plan.versions)
        self.assertEqual(('production',), plan.environments)
        self.assertFalse(plan.matches({'summary': 'JET-807 export version 4.6.2'}))
        self.assertFalse(plan.matches({'summary': 'JET-806 export version 4.6.20'}))

    def test_other_descriptive_modifiers_supply_their_words_to_matching(self):
        for modifier, head, source in (
                ('amber-line', 'coverage', 'The amber line coverage is pending.'),
                ('signal-window', 'review', 'The signal window review is complete.'),
                ('multi-stage', 'inspection', 'The multi stage inspection is pending.')):
            with self.subTest(modifier=modifier):
                query = f'What changed about {modifier} {head}?'
                plan = plan_query(query, self.project)
                self.assertEqual(query, plan.content_query)
                self.assertNotIn(modifier, plan.identifiers)
                self.assertTrue(plan.matches({'summary': source}))
                self.assertTrue(all(word in ' '.join(plan.candidate_queries)
                                    for word in modifier.split('-')))


    def test_code_shaped_modifier_keeps_internal_camel_case_identity(self):
        plan = plan_query('What changed about amberLine-cut export?', self.project)
        self.assertIn('amberline-cut', plan.identifiers)
        self.assertFalse(plan.matches({'summary': 'The amberline cut export changed.'}))


    def test_functional_adverbs_after_bare_unknown_scope_keep_identity(self):
        for adverb in ('now', 'today', 'yesterday', 'tomorrow', 'currently',
                       'here', 'there', 'then', 'again', 'however', 'instead',
                       'otherwise', 'therefore'):
            with self.subTest(adverb=adverb):
                plan = plan_query(f'What changed for foreign-board {adverb}?', self.project)
                self.assertIn('foreign-board', plan.identifiers)
                self.assertFalse(plan.matches({'summary': 'The foreign board coverage changed.'}))



if __name__ == '__main__':
    unittest.main()
