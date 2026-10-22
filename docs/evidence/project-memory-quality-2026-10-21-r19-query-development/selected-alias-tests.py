"""Selected project metadata does not require project names inside source text."""
from pathlib import Path
import unittest

from codex_mem.project_memory_query import plan_query


class QueryScopeContractTests(unittest.TestCase):
    owner = Path('/public/scope-contract/relay_workspace')
    neighbor = Path('/public/scope-contract/beacon-board')

    def test_selected_aliases_are_scope_in_unrecognized_natural_grammar(self):
        scopes = [self.owner, self.neighbor]
        for query in (
            'Across relay_workspace and beacon-board, what requirements remain active?',
            'What was the final relay_workspace instruction?',
            'For relay_workspace, compare retained obligations with the beacon-board instruction?',
            'Explain relay_workspace constraints and beacon-board constraints?',
        ):
            for owner in scopes:
                with self.subTest(query=query, owner=owner):
                    plan = plan_query(query, owner, selected_project_scopes=scopes)
                    self.assertFalse(plan.identifiers)
                    self.assertNotIn('relay_workspace', plan.content_query.casefold())
                    self.assertNotIn('beacon-board', plan.content_query.casefold())
                    self.assertTrue(plan.matches({'summary': 'Preserve the raw inventory records.'}))


    def test_literal_occurrences_are_not_stripped_even_beside_a_scope_mention(self):
        for literal in ('`relay_workspace`', '"relay_workspace"', "'relay_workspace'",
                        'function relay_workspace', 'module named relay_workspace',
                        'file called relay_workspace', 'artifact relay_workspace',
                        'relay_workspace()', 'relay_workspace.py',
                        'relay_workspace.member', '/exports/relay_workspace'):
            query = f'Within relay_workspace, what changed in {literal}?'
            with self.subTest(literal=literal):
                plan = plan_query(query, self.owner)
                self.assertTrue(plan.identifiers)
                self.assertFalse(plan.matches({'summary': 'Only an inventory receipt is present.'}))
                self.assertIn(literal.casefold(), plan.content_query.casefold())

    def test_commands_and_executable_labels_remain_literal(self):
        for literal in ('run relay_workspace', 'execute relay_workspace', 'invoke relay_workspace',
                        'command relay_workspace', 'executable named relay_workspace'):
            query = f'Within relay_workspace, what happened after {literal}?'
            with self.subTest(literal=literal):
                plan = plan_query(query, self.owner)
                self.assertIn('relay_workspace', plan.identifiers)
                self.assertIn(literal, plan.content_query)
                self.assertFalse(plan.matches({'summary': 'Only an inventory receipt is present.'}))

    def test_plain_unmarked_selected_nouns_keep_their_existing_content_strength(self):
        owner = Path('/public/scope-contract/catalogue')
        for query in ('What was the final catalogue instruction?', 'What did catalogue contain?',
                      'What does function catalogue establish?', 'What changed after run catalogue?'):
            with self.subTest(query=query):
                plan = plan_query(query, owner)
                self.assertIn('catalogue', plan.content_query)
                if 'function' in query or 'run' in query:
                    self.assertIn('catalogue', plan.identifiers)
                    self.assertFalse(plan.matches({'summary': 'Preserve the raw inventory records.'}))
                else:
                    self.assertNotIn('catalogue', plan.identifiers)
                    self.assertEqual((), plan.scope_aliases)
        for query in ('What is the current goal for catalogue?',
                      'Across catalogue and relay_workspace, what requirements remain active?'):
            plan = plan_query(query, owner, selected_project_scopes=[owner, self.owner])
            self.assertNotIn('catalogue', plan.identifiers)
            self.assertTrue(plan.matches({'summary': 'Preserve the raw inventory records.'}))

    def test_unknown_unselected_and_colliding_names_stay_literal(self):
        for query in ('What remains for missing_workspace?', 'What remains for beacon-board?'):
            plan = plan_query(query, self.owner)
            self.assertTrue(plan.identifiers)
            self.assertFalse(plan.matches({'summary': 'Preserve the raw inventory records.'}))
        collision = Path('/public/another-owner/relay_workspace')
        plan = plan_query('What was the final relay_workspace instruction?', self.owner,
                          selected_project_scopes=[self.owner, collision])
        self.assertIn('relay_workspace', plan.identifiers)
        self.assertEqual((), plan.scope_aliases)
        self.assertFalse(plan.matches({'summary': 'Preserve the raw inventory records.'}))

    def test_scope_removal_preserves_task_artifact_version_and_environment_constraints(self):
        query = ('Across relay_workspace and beacon-board, what did JET-806 `scan_rule` '
                 'version 4.6.2 establish in production, excluding staging?')
        plan = plan_query(query, self.owner, selected_project_scopes=[self.owner, self.neighbor])
        self.assertEqual(('jet-806',), plan.task_ids)
        self.assertEqual(('scan_rule',), plan.identifiers)
        self.assertEqual(('4.6.2',), plan.versions)
        self.assertEqual(('production',), plan.environments)
        self.assertEqual(('staging',), plan.excluded_environments)
        self.assertTrue(plan.matches({'summary': 'JET-806 scan_rule version 4.6.2',
                                      'scope': {'environment': 'production'}}))
        for text in ('JET-807 scan_rule version 4.6.2', 'JET-806 other_rule version 4.6.2',
                     'JET-806 scan_rule version 4.6.20'):
            self.assertFalse(plan.matches({'summary': text}))
        self.assertFalse(plan.matches({'summary': 'JET-806 scan_rule version 4.6.2',
                                       'scope': {'environment': 'staging'}}))

    def test_task_version_and_environment_named_projects_are_not_scope_aliases(self):
        for name in ('JET-806', '4.6.2', 'production'):
            plan = plan_query(f'What changed in {name}?', self.owner.parent / name)
            self.assertEqual((), plan.scope_aliases)
            self.assertIn(name.casefold(), plan.content_query.casefold())


if __name__ == '__main__':
    unittest.main()
