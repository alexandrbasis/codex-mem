"""Environment selection keeps user constraints distinct from execution evidence."""
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree

from codex_mem.config import configure
from codex_mem.project_memory import ProjectMemory
from codex_mem.project_memory_retrieval import build_project_brief
from codex_mem.store import Store


class EnvironmentConstraintQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = (Path(self.temp.name) / 'register').resolve()
        self.store = Store(Path(self.temp.name) / 'data')
        self.addCleanup(self.store.close)
        configure(self.store.data_dir, project_memory_mode='pilot', capture_scope='all',
                  project_memory_projects=[str(self.project)])

    def claim(self, body, *, environment='', role='hook:UserPromptSubmit', kind='constraint'):
        source = self.store.remember(self.project, 'Evidence', body, source=role, session_id='evidence')
        memory = ProjectMemory(self.store)
        job = memory.claim(self.project)
        ref = {'source_id': source['id'], 'start': 0, 'end': len(body), 'quote': body}
        memory.commit(job, {'claims': [{'key': 'claim', 'text': body, 'kind': kind,
            'workflow_state': 'open', 'scope': {'environment': environment}, 'evidence': {'text': [ref]}}]})
        return source

    def views(self, query):
        return (build_project_brief(self.store, self.project, query=query, budget=6000)['xml'],
                self.store.context(self.project, query=query, budget=12000,
                                   allow_remote=False, include_health=False))

    def test_local_user_constraint_is_not_removed_by_an_excluded_word_in_its_prohibition(self):
        source = self.claim('No production deployment.', environment='local')
        query = 'Which constraints remain active in local, excluding production?'
        for xml in self.views(query):
            self.assertIn(source['id'], xml)
            self.assertIn('source_role="user_intent"', xml)
            self.assertIn('No production deployment.', xml)

    def test_projectwide_user_prohibition_remains_unscoped(self):
        source = self.claim('No production deployment.')
        for query in ('Which constraints remain active in local?',
                      'Which constraints remain active in local, excluding production?'):
            for xml in self.views(query):
                self.assertIn(source['id'], xml)
                self.assertIn('source_role="user_intent"', xml)
            brief = build_project_brief(self.store, self.project, query=query)
            record = next(record for record in brief['claims']
                          if any(ref['source_id'] == source['id'] for field in record['fields']
                                 for ref in field['evidence']))
            self.assertIn(record.get('environment'), (None, ''))
            self.assertNotIn('execution', record)

    def test_mixed_environment_wording_in_user_constraint_keeps_complete_quote(self):
        body = 'Do not deploy to production; local validation only.'
        source = self.claim(body, environment='local')
        for xml in self.views('Which constraints remain active in local, excluding production?'):
            self.assertIn(source['id'], xml)
            self.assertIn(body, xml)

    def test_unscoped_user_report_stays_user_material_without_invented_local_execution(self):
        source = self.claim('Production deployment completed.', kind='fact')
        for xml in self.views('Which results are confirmed in local?'):
            self.assertIn(source['id'], xml)
            root = ElementTree.fromstring(xml)
            pointers = [pointer for pointer in root.findall('.//source')
                        if pointer.get('source_id') == source['id']]
            self.assertTrue(pointers)
            self.assertTrue(all(pointer.get('source_role') == 'user_intent' for pointer in pointers))
        brief = build_project_brief(self.store, self.project, query='Which results are confirmed in local?')
        for record in brief['records']:
            if any(pointer.get('source_id') == source['id'] for field in record.get('fields', [])
                   for pointer in field.get('evidence', [])):
                self.assertIn(record.get('environment'), (None, ''))
                self.assertNotIn('execution', record)

    def test_actual_opposite_scopes_are_excluded_for_every_source_role(self):
        for role in ('hook:UserPromptSubmit', 'hook:Stop', 'hook:PostToolUse'):
            with self.subTest(role=role):
                source = self.claim('Production deployment completed. ' + role,
                                    role=role, kind='result', environment='production')
                for xml in self.views('Which results are confirmed in local?'):
                    self.assertNotIn(source['id'], xml)

    def test_unscoped_execution_reports_keep_opposite_environment_guard(self):
        for role in ('hook:Stop', 'hook:PostToolUse'):
            with self.subTest(role=role):
                source = self.claim('Production deployment completed. ' + role, role=role, kind='result')
                for query in ('Which results are confirmed in local?',
                              'Which constraints remain active in local, excluding production?'):
                    for xml in self.views(query):
                        self.assertNotIn(source['id'], xml)

    def test_session_and_deletion_guards_still_remove_user_constraints(self):
        source = self.claim('No production deployment.', environment='local')
        query = 'Which constraints remain active in local, excluding production?'
        self.assertNotIn(source['id'], build_project_brief(self.store, self.project,
            query=query, exclude_session_id='evidence')['xml'])
        self.assertNotIn(source['id'], self.store.context(self.project, query=query,
            exclude_session='evidence', budget=12000, allow_remote=False, include_health=False))
        self.store.forget(self.project, [source['id']])
        for xml in self.views(query):
            self.assertNotIn(source['id'], xml)

    def test_version_identifier_guards_and_residual_topic_neutrality(self):
        source = self.claim('NEX-418 bundle_rule version 2.8.4. No production deployment.', environment='local')
        query = 'Which constraints remain active for NEX-418 bundle_rule version 2.8.4 in local?'
        self.assertIn(source['id'], build_project_brief(self.store, self.project, query=query)['xml'])
        for mismatch in (query.replace('NEX-418', 'NEX-419'), query.replace('bundle_rule', 'other_rule'),
                         query.replace('2.8.4', '2.8.40'),
                         'Which ultraviolet constraints remain active in local?'):
            with self.subTest(query=mismatch):
                for xml in self.views(mismatch):
                    from codex_mem.project_memory_query import plan_query
                    plan = plan_query(mismatch, self.project)
                    if plan.task_ids or plan.identifiers or plan.versions:
                        self.assertNotIn(source['id'], xml)
                    else:
                        for item in ElementTree.fromstring(xml).findall('item'):
                            if any(ref.get('source_id') == source['id'] for ref in item.iter('source')):
                                self.assertEqual(item.get('relationship_to_query'), 'unresolved')
                                self.assertEqual(item.get('is_requirement_claim'), 'False')
                                self.assertIsNone(item.get('workflow_state'))

    def test_mixed_episode_does_not_resurface_opposite_execution_via_a_user_pointer(self):
        user_body = 'No production deployment.'
        tool_body = 'Production deployment completed.'
        user = self.store.remember(self.project, 'Constraint', user_body,
                                   source='hook:UserPromptSubmit', session_id='mixed')
        tool = self.store.remember(self.project, 'Execution', tool_body,
                                   source='hook:PostToolUse', session_id='mixed')
        memory = ProjectMemory(self.store)
        job = memory.claim(self.project)
        user_ref = {'source_id': user['id'], 'start': 0, 'end': len(user_body), 'quote': user_body}
        tool_ref = {'source_id': tool['id'], 'start': 0, 'end': len(tool_body), 'quote': tool_body}
        memory.commit(job, {'episodes': [{'key': 'mixed', 'summary': user_body + ' ' + tool_body,
            'goal': user_body, 'outcome': tool_body,
            'evidence': {'summary': [user_ref, tool_ref], 'goal': [user_ref], 'outcome': [tool_ref]}}]})
        for xml in self.views('Which constraints remain active in local, excluding production?'):
            self.assertIn(user['id'], xml)
            self.assertNotIn(tool['id'], xml)

    def test_pending_user_material_keeps_unknown_scope_and_provisional_state(self):
        source = self.store.remember(self.project, 'Pending user instruction',
            'No production deployment.', source='hook:UserPromptSubmit', session_id='pending')
        brief = build_project_brief(self.store, self.project,
            query='Which constraints remain active in local, excluding production?')
        self.assertIn(source['id'], brief['xml'])
        signal = next(signal for signal in brief['signals'] if signal['id'] == source['id'])
        self.assertTrue(signal['provisional'])
        self.assertEqual('user_intent', signal['source_role'])
        self.assertIn(signal.get('environment'), (None, ''))
        self.assertNotIn('execution', signal)

    def test_opposite_scope_still_excludes_normative_user_constraint(self):
        source = self.claim('No production deployment.', environment='production')
        for xml in self.views('Which constraints remain active in local, excluding production?'):
            self.assertNotIn(source['id'], xml)

    def test_non_environment_version_exclusion_and_quoted_literals_stay_strict(self):
        source = self.claim('NEX-418 bundle_rule version 2.8.4. No production deployment.', environment='local')
        for query in ('Show NEX-418 bundle_rule in local, excluding version 2.8.4.',
                      'Which constraints remain active for NEX-418 `other_rule` in local?'):
            for xml in self.views(query):
                self.assertNotIn(source['id'], xml)

    def test_inferred_result_cannot_hide_the_environment_of_its_tool_evidence(self):
        body = 'Production deployment completed.'
        source = self.store.remember(self.project, 'Tool result', body,
            source='hook:PostToolUse', session_id='inferred')
        memory = ProjectMemory(self.store)
        job = memory.claim(self.project)
        ref = {'source_id': source['id'], 'start': 0, 'end': len(body), 'quote': body}
        memory.commit(job, {'claims': [{'key': 'result', 'kind': 'result',
            'workflow_state': 'open', 'text': 'The deployment operation succeeded.',
            'evidence': {'text': [ref]}}]})
        for xml in self.views('Which results are confirmed in local?'):
            self.assertNotIn(source['id'], xml)

    def test_unresolved_custody_keeps_the_full_user_source_despite_environment_words(self):
        body = 'No production deployment. Record the missing review.'
        quote = 'No production deployment.'
        source = self.store.remember(self.project, 'Partially represented user source', body,
            source='hook:UserPromptSubmit', session_id='custody')
        memory = ProjectMemory(self.store)
        job = memory.claim(self.project)
        ref = {'source_id': source['id'], 'start': 0, 'end': len(quote), 'quote': quote}
        memory.commit(job, {'episodes': [{'key': 'partial', 'summary': quote, 'goal': quote,
                                         'evidence': {'summary': [ref], 'goal': [ref]}}]})
        query = 'Which constraints remain active in local, excluding production?'
        brief = build_project_brief(self.store, self.project, query=query)
        # A separate original-source bundle can already make the text visible.
        # The neutral custody lane must independently retain its own state.
        custody = next(record for record in brief['intent_custody']
                       if any(pointer['source_id'] == source['id'] for field in record['fields']
                              for pointer in field['evidence']))
        self.assertEqual('unresolved', custody['disposition'])
        self.assertFalse(custody['is_requirement_claim'])
        pointer = custody['fields'][0]['evidence'][0]
        self.assertEqual('user_intent', pointer['source_role'])
        self.assertEqual(body, pointer['quote_excerpt'])
        self.assertEqual((0, len(body)), (pointer['start'], pointer['end']))
        self.assertIn(custody.get('environment'), (None, ''))
        for xml in self.views(query):
            self.assertIn(body, xml)

    def test_custody_still_obeys_literal_version_topic_session_and_deletion_guards(self):
        first = 'NEX-418 bundle_rule version 2.8.4.'
        body = first + ' No production deployment. A review remains pending.'
        source = self.store.remember(self.project, 'Partially represented scoped source', body,
            source='hook:UserPromptSubmit', session_id='custody-guards')
        memory = ProjectMemory(self.store)
        job = memory.claim(self.project)
        ref = {'source_id': source['id'], 'start': 0, 'end': len(first), 'quote': first}
        memory.commit(job, {'episodes': [{'key': 'partial', 'summary': first, 'goal': first,
            'evidence': {'summary': [ref], 'goal': [ref]}}]})
        query = 'Which constraints remain active for NEX-418 bundle_rule version 2.8.4 in local?'
        def custody_ids(brief):
            return {pointer['source_id'] for record in brief['intent_custody']
                    for field in record['fields'] for pointer in field['evidence']}
        self.assertIn(source['id'], custody_ids(build_project_brief(self.store, self.project, query=query)))
        for mismatch in (query.replace('NEX-418', 'NEX-419'),
                         query.replace('bundle_rule', '`other_rule`'),
                         query.replace('2.8.4', '2.8.40'),
                         'Show NEX-418 in local, excluding version 2.8.4.',
                         'Which ultraviolet constraints remain active in local?'):
            with self.subTest(query=mismatch):
                brief = build_project_brief(self.store, self.project, query=mismatch)
                self.assertNotIn(source['id'], custody_ids(brief))
        excluded = build_project_brief(self.store, self.project, query=query,
                                       exclude_session_id='custody-guards')
        self.assertNotIn(source['id'], custody_ids(excluded))
        self.store.forget(self.project, [source['id']])
        self.assertNotIn(source['id'], custody_ids(build_project_brief(self.store, self.project, query=query)))

    def test_partial_scoped_claim_does_not_assign_scope_to_unrepresented_user_material(self):
        first = 'No production deployment.'
        body = first + ' Keep the review manual.'
        source = self.store.remember(self.project, 'Partially scoped source', body,
            source='hook:UserPromptSubmit', session_id='partial-scope')
        memory = ProjectMemory(self.store)
        job = memory.claim(self.project)
        ref = {'source_id': source['id'], 'start': 0, 'end': len(first), 'quote': first}
        memory.commit(job, {'claims': [{'key': 'partial', 'kind': 'constraint',
            'workflow_state': 'open', 'text': first, 'scope': {'environment': 'production'},
            'evidence': {'text': [ref]}}]})
        brief = build_project_brief(self.store, self.project,
            query='Which constraints remain active in local, excluding production?')
        custody = next(record for record in brief['intent_custody']
                       if any(pointer['source_id'] == source['id'] for field in record['fields']
                              for pointer in field['evidence']))
        self.assertEqual('unresolved', custody['disposition'])
        self.assertFalse(custody['is_requirement_claim'])
        self.assertEqual(body, custody['fields'][0]['evidence'][0]['quote_excerpt'])
        self.assertIn(custody.get('environment'), (None, ''))

    def test_identical_text_other_source_does_not_assign_custody_scope(self):
        body = 'No production deployment. Keep reviews manual.'
        original = self.store.remember(self.project, 'Original user source', body,
            source='hook:UserPromptSubmit', session_id='same-text')
        other = self.store.remember(self.project, 'Other user source', body,
            source='hook:UserPromptSubmit', session_id='same-text')
        self.assertNotEqual(original['id'], other['id'])
        memory = ProjectMemory(self.store)
        job = memory.claim(self.project)
        ref = {'source_id': other['id'], 'start': 0, 'end': len(body), 'quote': body}
        memory.commit(job, {'claims': [{'key': 'other', 'kind': 'result', 'workflow_state': 'open',
            'text': body, 'scope': {'environment': 'production'}, 'evidence': {'text': [ref]}}]})
        brief = build_project_brief(self.store, self.project,
            query='Which constraints remain active in local, excluding production?')
        ids = {pointer['source_id'] for record in brief['intent_custody']
               for field in record['fields'] for pointer in field['evidence']}
        self.assertIn(original['id'], ids)
        self.assertNotIn(other['id'], ids)

    def test_mixed_source_claim_does_not_assign_scope_to_a_whole_user_source(self):
        body = 'No production deployment. Keep reviews manual.'
        source = self.store.remember(self.project, 'Original source', body,
            source='hook:UserPromptSubmit', session_id='mixed-binding')
        extra_body = 'This separate result was reported for production.'
        extra = self.store.remember(self.project, 'Other source', extra_body,
            source='hook:UserPromptSubmit', session_id='mixed-binding')
        memory = ProjectMemory(self.store)
        job = memory.claim(self.project)
        refs = [{'source_id': record['id'], 'start': 0, 'end': len(text), 'quote': text}
                for record, text in ((source, body), (extra, extra_body))]
        memory.commit(job, {'claims': [{'key': 'mixed', 'kind': 'result', 'workflow_state': 'open',
            'text': body, 'scope': {'environment': 'production'}, 'evidence': {'text': refs}}]})
        brief = build_project_brief(self.store, self.project,
            query='Which constraints remain active in local, excluding production?')
        self.assertTrue(any(pointer['source_id'] == source['id'] for record in brief['intent_custody']
                            for field in record['fields'] for pointer in field['evidence']))

    def test_conflicting_whole_source_scopes_are_not_unrestricted_custody(self):
        body = 'No production deployment. Keep reviews manual.'
        source = self.store.remember(self.project, 'Conflicting associations', body,
            source='hook:UserPromptSubmit', session_id='conflict')
        memory = ProjectMemory(self.store)
        job = memory.claim(self.project)
        ref = {'source_id': source['id'], 'start': 0, 'end': len(body), 'quote': body}
        memory.commit(job, {'claims': [{'key': environment, 'kind': 'result', 'workflow_state': 'open',
            'text': body, 'scope': {'environment': environment}, 'evidence': {'text': [ref]}}
            for environment in ('local', 'production')]})
        for environment in ('local', 'production'):
            brief = build_project_brief(self.store, self.project,
                query='Which results are confirmed?', environment=environment)
            self.assertEqual(1, brief['coverage']['custody_scope_conflicts'])
            self.assertFalse(any(pointer['source_id'] == source['id'] for record in brief['intent_custody']
                                 for field in record['fields'] for pointer in field['evidence']))
        unscoped = build_project_brief(self.store, self.project, query='Which results are confirmed?')
        custody = next(record for record in unscoped['intent_custody']
                       if any(pointer['source_id'] == source['id'] for field in record['fields']
                              for pointer in field['evidence']))
        self.assertEqual('conflicting_declared_environment_scopes', custody['uncertainty'])
        self.assertIn(custody.get('environment'), (None, ''))

    def test_superseded_claim_cannot_bind_custody_but_hidden_transition_does_not_retire_it(self):
        body = 'SCP-44 approval'
        source = self.claim(body, environment='production', kind='result')
        memory = ProjectMemory(self.store)
        target = memory.snapshot(self.project)['claims'][0]['id']
        replacement_body = 'SCP-44 approval replaced after a new review.'
        replacement = self.store.remember(self.project, 'Replacement', replacement_body,
            source='hook:UserPromptSubmit', session_id='replacement')
        job = memory.claim(self.project)
        ref = {'source_id': replacement['id'], 'start': 0, 'end': len(replacement_body), 'quote': replacement_body}
        memory.commit(job, {'claims': [{'key': 'new', 'kind': 'correction', 'workflow_state': 'open',
            'text': replacement_body, 'scope': {'environment': 'production'}, 'evidence': {'text': [ref]}}],
            'relations': [{'from': 'new', 'to': target, 'type': 'replaces', 'evidence': [ref]}]})
        query = 'Which results are confirmed in local?'
        def ids(brief):
            return {pointer['source_id'] for record in brief['intent_custody']
                    for field in record['fields'] for pointer in field['evidence']}
        self.assertIn(source['id'], ids(build_project_brief(self.store, self.project, query=query)))
        self.assertNotIn(source['id'], ids(build_project_brief(self.store, self.project,
            query=query, exclude_session_id='replacement')))

    def assert_custody_uncertainty_order(self, conflict_first):
        bodies = {'ordinary': 'Review ordinary shelf notes.', 'conflict': 'Review scoped shelf notes.'}
        order = ('conflict', 'ordinary') if conflict_first else ('ordinary', 'conflict')
        sources = {name: self.store.remember(self.project, name, bodies[name],
            source='hook:UserPromptSubmit', session_id='uncertainty-order') for name in order}
        memory = ProjectMemory(self.store)
        job = memory.claim(self.project)
        source = sources['conflict']
        body = bodies['conflict']
        ref = {'source_id': source['id'], 'start': 0, 'end': len(body), 'quote': body}
        memory.commit(job, {'claims': [{'key': environment, 'kind': 'result', 'workflow_state': 'open',
            'text': body, 'scope': {'environment': environment}, 'evidence': {'text': [ref]}}
            for environment in ('local', 'production')]})
        brief = build_project_brief(self.store, self.project, query='Which results are confirmed?')
        first = brief['intent_custody'][0]['fields'][0]['evidence'][0]['source_id']
        self.assertEqual(sources[order[0]]['id'], first)
        for xml in (brief['xml'], self.store.context(self.project, query='Which results are confirmed?',
                         budget=6000, allow_remote=False, include_health=False)):
            self.assertLessEqual(len(xml), 6000)
            root = ElementTree.fromstring(xml)
            for name, expected in (('ordinary', 'extracted_representation_completeness_unresolved'),
                                   ('conflict', 'conflicting_declared_environment_scopes')):
                matching = [item for item in root.findall('.//item')
                            if item.get('lane') == 'intent_custody' and any(
                                pointer.get('source_id') == sources[name]['id']
                                for pointer in item.findall('.//source'))]
                self.assertTrue(matching, name)
                self.assertTrue(all(item.get('uncertainty') == expected for item in matching), name)
                self.assertEqual(1, sum(pointer.get('source_id') == sources[name]['id']
                                        for item in matching for pointer in item.findall('.//source')))
                for item in matching:
                    for pointer in item.findall('.//source'):
                        if pointer.get('source_id') == sources[name]['id']:
                            self.assertEqual(bodies[name], pointer.findtext('quote_excerpt'))
                            self.assertEqual(('0', str(len(bodies[name]))),
                                             (pointer.get('start'), pointer.get('end')))

    def test_custody_rendering_preserves_second_source_conflict_uncertainty(self):
        self.assert_custody_uncertainty_order(conflict_first=False)

    def test_custody_rendering_does_not_copy_first_source_conflict_to_ordinary_source(self):
        self.assert_custody_uncertainty_order(conflict_first=True)
