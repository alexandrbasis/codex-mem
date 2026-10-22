"""Selected project metadata does not require project names inside source text."""
from pathlib import Path
import unittest

from codex_mem.project_memory_query import plan_query


class QueryScopeContractTests(unittest.TestCase):
    owner = Path('/public/scope-contract/relay_workspace')
    neighbor = Path('/public/scope-contract/beacon-board')

    def test_selected_scope_preface_classifies_period_ended_natural_request_body(self):
        plain = self.owner.parent / 'catalogue'
        for owner in (self.owner, plain):
            for verb in ('compare', 'contrast'):
                query = f'For {owner.name}, {verb} the earlier request with the final instruction.'
                with self.subTest(owner=owner, verb=verb):
                    plan = plan_query(query, owner)
                    self.assertTrue(plan.natural)
                    self.assertEqual((owner.name.casefold(),), plan.scope_aliases)
                    self.assertEqual(f'{verb} the earlier request with the final instruction.', plan.content_query)
                    self.assertFalse(plan.identifiers)
                    self.assertTrue(plan.matches({'summary': 'Preserve the raw inventory records.'}))
        for prep in ('Within', 'In', 'Across'):
            query = f'{prep} relay_workspace and beacon-board, compare the earlier request with the final instruction.'
            plan = plan_query(query, self.owner, selected_project_scopes=[self.owner, self.neighbor])
            self.assertTrue(plan.natural)
            self.assertEqual({'relay_workspace', 'beacon-board'}, set(plan.scope_aliases))
            self.assertFalse(plan.identifiers)

    def test_scope_preface_does_not_relax_literal_lookup_or_untrusted_prefix(self):
        for remainder in ('bundle_rule.', 'JET-806 4.6.2.', '`scan_rule`.',
                          'relay_workspace --raw.', 'function relay_workspace.',
                          'compare_rule.', 'showThing().', 'list().', 'compare().', 'contrast().'):
            query = f'For relay_workspace, {remainder}'
            with self.subTest(remainder=remainder):
                plan = plan_query(query, self.owner)
                self.assertFalse(plan.natural)
                self.assertEqual((), plan.scope_aliases)
                self.assertEqual(query, plan.content_query)
        for prefix in ('missing_workspace', 'beacon-board', '`relay_workspace`', '"relay_workspace"'):
            query = f'For {prefix}, compare the earlier request with the final instruction.'
            plan = plan_query(query, self.owner)
            self.assertFalse(plan.natural)
            self.assertEqual((), plan.scope_aliases)
            self.assertEqual(query, plan.content_query)
        for name in ('JET-806', '4.6.2', 'production'):
            plan = plan_query(f'For {name}, compare the earlier request with the final instruction.',
                              self.owner.parent / name)
            self.assertFalse(plan.natural)
            self.assertEqual((), plan.scope_aliases)
        collision = self.owner.parent / 'another' / self.owner.name
        plan = plan_query(f'For {self.owner.name}, compare the earlier and final instruction.', self.owner,
                          selected_project_scopes=[self.owner, collision])
        self.assertFalse(plan.natural)
        self.assertEqual((), plan.scope_aliases)

    def test_natural_scope_preface_keeps_literal_body_and_exact_subject_constraints(self):
        plan = plan_query('For relay_workspace, compare `scan_rule` for JET-806 version 4.6.2 in production.', self.owner)
        self.assertTrue(plan.natural)
        self.assertEqual(('jet-806',), plan.task_ids)
        self.assertEqual(('scan_rule',), plan.identifiers)
        self.assertEqual(('4.6.2',), plan.versions)
        self.assertEqual(('production',), plan.environments)
        for wrong in ('JET-807 scan_rule version 4.6.2', 'JET-806 other_rule version 4.6.2',
                      'JET-806 scan_rule version 4.6.20'):
            self.assertFalse(plan.matches({'summary': wrong}))
        for literal in ('command relay_workspace', 'executable named relay_workspace',
                        '`relay_workspace`', 'relay_workspace()', '/exports/relay_workspace'):
            plan = plan_query(f'For relay_workspace, show {literal}.', self.owner)
            self.assertTrue(plan.natural)
            self.assertTrue(plan.identifiers)
            self.assertFalse(plan.matches({'summary': 'Only an inventory receipt is present.'}))
        plain = self.owner.parent / 'catalogue'
        plan = plan_query('For catalogue, compare function catalogue with the last instruction.', plain)
        self.assertIn('catalogue', plan.identifiers)
        self.assertFalse(plan.matches({'summary': 'Only an inventory receipt is present.'}))

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

    def test_bare_conjunction_keeps_hyphenated_description_soft_and_present(self):
        query = 'What is the final ticket status and bench-calibration scope in relay_workspace?'
        plan = plan_query(query, self.owner)
        self.assertNotIn('bench-calibration', plan.identifiers)
        self.assertIn('bench-calibration', plan.content_query)
        self.assertTrue(plan.matches({'summary': 'The final check covered only the preview.'}))
        self.assertTrue(any('bench' in window or 'calibr' in window for window in plan.candidate_queries))

    def test_soft_compound_planning_does_not_rewrite_source_text_or_intervals(self):
        from copy import deepcopy
        from xml.etree import ElementTree
        from codex_mem.project_memory_retrieval import render_project_brief
        query = 'What is the final ticket status and bench-calibration scope in relay_workspace?'
        plan = plan_query(query, self.owner)
        self.assertEqual('What is the final ticket status and bench-calibration scope ?', plan.content_query)
        text = 'bench-calibration --raw JET-806 version 4.6.2 in production: status pending.'
        pointer = {'source_id': 'public-calibration', 'source_role': 'tool_record',
                   'source_field': 'body', 'start': 0, 'end': len(text),
                   'quote_excerpt': text, 'quote_truncated': False}
        record = {'id': 'canonical-calibration', 'lane': 'episode', 'summary': text,
                  'fields': [{'name': 'outcome', 'basis': 'attributed_source_excerpts',
                              'field_association': 'model_inference', 'evidence': [pointer]}]}
        before = deepcopy(record)
        self.assertTrue(plan.matches(record))
        xml = render_project_brief({'records': [record]}, budget=2900)
        sources = list(ElementTree.fromstring(xml).iter('source'))
        self.assertTrue(sources, xml)
        self.assertTrue(any(source.findtext('quote_excerpt') == text and
            source.get('source_id') == pointer['source_id'] and source.get('source_role') == 'tool_record' and
            source.get('source_field') == 'body' and source.get('start') == '0' and
            source.get('end') == str(len(text)) for source in sources), xml)
        self.assertEqual(before, record)

    def test_unknown_coordinated_scope_stays_hard_after_selected_name_is_removed(self):
        for query in ('Across relay_workspace and foreign-board, what changed?',
                      'What changed for relay_workspace and foreign-board?',
                      'Compare relay_workspace and foreign-board?',
                      'What changed in relay_workspace and beacon-board and foreign-board?',
                      'Across relay_workspace, beacon-board, and foreign-board, what changed?'):
            with self.subTest(query=query):
                plan = plan_query(query, self.owner, selected_project_scopes=[self.owner, self.neighbor])
                self.assertNotIn('relay_workspace', plan.identifiers)
                self.assertIn('foreign-board', plan.identifiers)
                self.assertFalse(plan.matches({'summary': 'Only an inventory receipt is present.'}))
        for query in ('What changed for foreign-board and relay_workspace?',
                      'What changed for foreign-board?',
                      'What changed for foreign-board and archive-board?'):
            plan = plan_query(query, self.owner)
            self.assertIn('foreign-board', plan.identifiers)
            if 'archive-board' in query:
                self.assertIn('archive-board', plan.identifiers)
            self.assertFalse(plan.matches({'summary': 'Only an inventory receipt is present.'}))

    def test_hyphenated_literals_keep_exact_matching_strength(self):
        for literal in ('`bench-calibration`', '"bench-calibration"', 'module bench-calibration',
                        'command bench-calibration', 'function bench-calibration',
                        'bench-calibration.py', 'JET-806'):
            with self.subTest(literal=literal):
                plan = plan_query(f'What is the final status and {literal} scope in relay_workspace?', self.owner)
                self.assertFalse(plan.matches({'summary': 'Only an inventory receipt is present.'}))

    def test_task_version_and_environment_named_projects_are_not_scope_aliases(self):
        for name in ('JET-806', '4.6.2', 'production'):
            plan = plan_query(f'What changed in {name}?', self.owner.parent / name)
            self.assertEqual((), plan.scope_aliases)
            self.assertIn(name.casefold(), plan.content_query.casefold())


if __name__ == '__main__':
    unittest.main()
