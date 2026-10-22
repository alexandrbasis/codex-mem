"""Public project reads distinguish selected scope names from literal subjects."""
from pathlib import Path
import tempfile
import unittest

from codex_mem.config import configure
from codex_mem.project_memory import ProjectMemory
from codex_mem.project_memory_retrieval import build_project_brief
from codex_mem.store import Store


class ProjectScopeQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.project = root / 'lot_ledger'
        self.neighbor = root / 'label-board'
        self.duplicate = root / 'other' / 'lot_ledger'
        self.store = Store(root / 'data')
        self.addCleanup(self.store.close)
        configure(self.store.data_dir, project_memory_mode='pilot', capture_scope='all',
                  project_memory_projects=[str(p) for p in (self.project, self.neighbor, self.duplicate)])
        self.primary = self.add_episode(self.project,
            'Recover the warehouse catalogue. Cancel the illustrated index. '
            'Replacement: Produce a text index. Still binding: Preserve original item numbers.', 'primary')
        self.other = self.add_episode(self.neighbor,
            'The printing board requires glossy stock. This permission stays within the printing project.', 'neighbor')

    def add_episode(self, project, text, session, *, role='hook:UserPromptSubmit'):
        source = self.store.remember(project, 'User instruction', text,
                                     source=role, session_id=session)
        memory = ProjectMemory(self.store)
        job = memory.claim(project)
        ref = {'source_id': source['id'], 'start': 0, 'end': len(text), 'quote': text}
        memory.commit(job, {'episodes': [{'key': 'work', 'summary': text, 'goal': text,
            'evidence': {'summary': [ref], 'goal': [ref]}}]})
        return source

    def test_natural_current_scope_alias_does_not_need_to_appear_in_source_text(self):
        brief = build_project_brief(self.store, self.project,
            query='What is the current goal for lot_ledger?', budget=6000)
        self.assertIn(self.primary['id'], brief['xml'])
        self.assertNotIn(self.other['id'], brief['xml'])

    def test_requirement_state_fields_are_not_topical_predicates(self):
        query = ('Which requirement was canceled in lot_ledger, what replaced it, '
                 'and which constraint remains active?')
        brief = build_project_brief(self.store, self.project, query=query, budget=6000)
        self.assertIn('Replacement: Produce a text index.', brief['xml'])
        self.assertIn('Still binding: Preserve original item numbers.', brief['xml'])
        topical = query.replace('requirement was', 'ultraviolet requirement was')
        unknown = build_project_brief(self.store, self.project, query=topical, budget=6000)
        self.assertFalse(unknown['episodes'])
        self.assertFalse(unknown['claims'])
        self.assertIn(self.primary['id'], unknown['xml'])
        self.assertIn('selection_basis="unassociated_user_context"', unknown['xml'])
        self.assertIn('relationship_to_query="unresolved"', unknown['xml'])
        self.assertIn('is_requirement_claim="False"', unknown['xml'])
        self.assertNotIn(self.other['id'], unknown['xml'])

    def test_selected_project_boundary_reads_stay_in_their_owner_scope(self):
        query = 'Does the requirement from label-board carry over to lot_ledger? Explain the project boundary.'
        scopes = [self.project, self.neighbor]
        for owner, expected, excluded in ((self.project, self.primary, self.other),
                                          (self.neighbor, self.other, self.primary)):
            brief = build_project_brief(self.store, owner, query=query, budget=6000,
                                        selected_project_scopes=scopes)
            self.assertIn(expected['id'], brief['xml'])
            self.assertNotIn(excluded['id'], brief['xml'])
            self.assertEqual(str(owner.resolve()), brief['project'])

    def test_literal_unknown_unselected_and_ambiguous_aliases_stay_constraints(self):
        queries = ('What is the goal for `lot_ledger`?', 'What is the goal for "lot_ledger"?',
                   "What is the goal for 'lot_ledger'?", 'What is the goal for missing_scope?',
                   'What is the goal for label-board?', 'What does function lot_ledger establish?')
        for query in queries:
            with self.subTest(query=query):
                brief = build_project_brief(self.store, self.project, query=query, budget=6000)
                self.assertNotIn(self.primary['id'], brief['xml'])
        ambiguous = build_project_brief(self.store, self.project,
            query='What is the current goal for lot_ledger?', budget=6000,
            selected_project_scopes=[self.project, self.duplicate])
        self.assertNotIn(self.primary['id'], ambiguous['xml'])

    def test_scope_lists_require_enabled_absolute_paths(self):
        for scopes in ('lot_ledger', [Path('relative')], [self.project.parent / 'disabled'],
                       [self.project] * 17):
            with self.subTest(scopes=scopes), self.assertRaises(ValueError):
                build_project_brief(self.store, self.project, query='What is current?',
                                    selected_project_scopes=scopes)

    def test_store_context_uses_same_scope_plan_without_cross_project_records(self):
        query = 'Does the requirement from label-board carry over to lot_ledger? Explain the project boundary.'
        xml = self.store.context(self.project, query=query, budget=12000,
            selected_project_scopes=[self.project, self.neighbor], allow_remote=False, include_health=False)
        self.assertIn(self.primary['id'], xml)
        self.assertNotIn(self.other['id'], xml)
        missing = self.store.context(self.project, query='Which ultraviolet requirement was canceled in lot_ledger?',
                                    budget=12000, allow_remote=False, include_health=False)
        self.assertNotIn(self.primary['id'], missing)

    def test_mcp_project_state_accepts_explicit_selected_scopes(self):
        from codex_mem.mcp import MemoryMCPServer, TOOLS
        schema = next(tool for tool in TOOLS if tool.name == 'memory_project_state').input_schema
        self.assertIn('selected_project_scopes', schema['properties'])
        server = MemoryMCPServer(store=self.store)
        result = server._execute_tool('memory_project_state', {
            'project': str(self.project), 'query': 'What is the current goal for lot_ledger?',
            'selected_project_scopes': [str(self.project), str(self.neighbor)]})
        self.assertIn(self.primary['id'], result['xml'])
        self.assertNotIn(self.other['id'], result['xml'])

    def test_two_selected_scopes_support_plain_comparison_and_scope_prepositions(self):
        for query in ('Compare lot_ledger with label-board?',
                      'Compare lot_ledger and label-board?'):
            for owner, expected, excluded in ((self.project, self.primary, self.other),
                                              (self.neighbor, self.other, self.primary)):
                with self.subTest(query=query, owner=owner):
                    brief = build_project_brief(self.store, owner, query=query,
                        selected_project_scopes=[self.project, self.neighbor])
                    self.assertIn(expected['id'], brief['xml'])
                    self.assertNotIn(excluded['id'], brief['xml'])
        for prep in ('for', 'in', 'of', 'on'):
            brief = build_project_brief(self.store, self.project,
                query=f'What is the current goal {prep} lot_ledger?')
            self.assertIn(self.primary['id'], brief['xml'])

    def test_new_scope_selection_keeps_session_exclusion_and_deletion(self):
        query = 'Which requirement was canceled in lot_ledger, what replaced it, and which constraint remains active?'
        excluded = build_project_brief(self.store, self.project, query=query, exclude_session_id='primary')
        self.assertNotIn(self.primary['id'], excluded['xml'])
        self.store.forget(self.project, [self.primary['id']])
        deleted = build_project_brief(self.store, self.project, query=query)
        self.assertNotIn(self.primary['id'], deleted['xml'])

    def test_scope_selection_keeps_task_identifier_version_and_environment_guards(self):
        source = self.add_episode(self.project,
            'NEX-418 bundle_rule version 2.8.4 passed in production.', 'receipt', role='hook:PostToolUse')
        query = 'What does NEX-418 bundle_rule version 2.8.4 establish for lot_ledger in production?'
        brief = build_project_brief(self.store, self.project, query=query)
        self.assertIn(source['id'], brief['xml'])
        for wrong in (query.replace('NEX-418', 'NEX-419'),
                      query.replace('bundle_rule', 'other_rule'),
                      query.replace('2.8.4', '2.8.40'),
                      query.replace('in production', 'in staging')):
            with self.subTest(query=wrong):
                self.assertNotIn(source['id'], build_project_brief(self.store, self.project, query=wrong)['xml'])

    def test_scope_names_in_quoted_or_artifact_positions_are_literal_even_without_punctuation(self):
        plain = self.project.parent / 'catalogue'
        configure(self.store.data_dir, project_memory_mode='pilot', capture_scope='all',
                  project_memory_projects=[str(self.project), str(self.neighbor), str(plain)])
        source = self.add_episode(plain, 'Recover the warehouse register.', 'plain-name')
        for query in ('What is the goal for "catalogue"?', 'What does function catalogue establish?',
                      'What does file named catalogue establish?'):
            with self.subTest(query=query):
                self.assertNotIn(source['id'], build_project_brief(self.store, plain, query=query)['xml'])
        self.assertIn(source['id'], build_project_brief(self.store, plain,
                                                      query='What is the current goal for catalogue?')['xml'])
