"""Grammatical answer facets preserve residual topics and literal read scopes."""
from pathlib import Path
import unittest

from codex_mem.project_memory_query import plan_query


class QueryFacetTests(unittest.TestCase):
    project = Path('/tmp/public-query-facets')

    def test_requested_output_deferral_is_a_facet_but_named_output_stays_topical(self):
        generic = plan_query('Which requested output was deferred?', self.project)
        self.assertEqual('', generic.answer_subject)
        self.assertEqual(('requirements', 'current_revision'), generic.answer_facets)
        self.assertEqual(('',), generic.candidate_queries)
        self.assertTrue(generic.generic_project_state)
        topic = plan_query('Which requested telescope output was deferred?', self.project)
        self.assertEqual('telescope', topic.answer_subject)
        self.assertEqual(('telescope',), topic.candidate_queries)
        self.assertFalse(topic.generic_project_state)

    def test_execution_ordinals_are_identity_facets_not_result_topics(self):
        for ordinal in ('first', 'initial', 'earliest'):
            with self.subTest(ordinal=ordinal):
                plan = plan_query(f'Did the {ordinal} check pass?', self.project)
                self.assertEqual('', plan.answer_subject)
                self.assertIn('execution_assessment', plan.answer_facets)
                self.assertEqual('earliest', plan.source_order)
                self.assertTrue(plan.assesses_current_evidence)
                self.assertFalse(plan.historical)
                self.assertEqual(('',), plan.candidate_queries)
        latest = plan_query('Was the latest spectrometer test successful?', self.project)
        self.assertEqual('spectrometer', latest.answer_subject)
        self.assertEqual('latest', latest.source_order)
        self.assertEqual(('spectrometer',), latest.candidate_queries)
        literal = plan_query('What does `first_packet` require?', self.project)
        self.assertIsNone(literal.source_order)
        self.assertIn('first_packet', literal.identifiers)

    def test_operation_recording_and_transitive_assessment_keep_subjects(self):
        cases = [
            ('How should the first ordering check be recorded?', 'ordering', 'earliest'),
            ('Did the initial correction test preserve numbering?', 'correction numbering', 'earliest'),
            ('How should the latest telescope run be classified?', 'telescope', 'latest'),
            ('Did the earliest telescope test retain calibration?', 'telescope calibration', 'earliest'),
        ]
        for question, subject, order in cases:
            with self.subTest(question=question):
                plan = plan_query(question, self.project)
                self.assertEqual(subject, plan.answer_subject)
                self.assertEqual(order, plan.source_order)
                self.assertIn('execution_assessment', plan.answer_facets)
                self.assertFalse(plan.historical)
        historical = plan_query('How should the first ordering check be recorded as of yesterday?', self.project)
        self.assertIsNone(historical.source_order)
        quoted = plan_query('How should `first_check` be recorded?', self.project)
        self.assertIsNone(quoted.source_order)
        self.assertIn('first_check', quoted.identifiers)

    def test_requested_disposition_retains_named_subject_and_hard_scope(self):
        plan = plan_query('What should happen to thumbnail previews?', self.project)
        self.assertEqual('thumbnail previews', plan.answer_subject)
        self.assertIn('current_revision', plan.answer_facets)
        self.assertFalse(plan.generic_project_state)
        unrelated = plan_query('What should happen to telescope previews?', self.project)
        self.assertEqual('telescope previews', unrelated.answer_subject)
        scoped = plan_query('What should happen to `preview_packet` version 2.8.4 in local?', self.project)
        self.assertEqual(('preview_packet',), scoped.identifiers)
        self.assertEqual(('2.8.4',), scoped.versions)
        self.assertEqual(('local',), scoped.environments)
        self.assertFalse(scoped.matches({'text': 'preview_other 2.8.4'}))

    def test_current_requirement_and_reason_frames_preserve_the_actual_subject(self):
        cases = [('What export remains requested for Vega?', 'export Vega'),
                 ('What remains before Vega can be sent?', 'Vega'),
                 ('Which teaching sequence remains required?', 'teaching sequence'),
                 ('What privacy rule applies and why?', 'privacy')]
        for query, subject in cases:
            with self.subTest(query=query):
                plan = plan_query(query, self.project)
                self.assertEqual(subject, plan.answer_subject)
                self.assertIn('current_revision', plan.answer_facets)
                self.assertFalse(plan.generic_project_state)
                self.assertTrue(plan.topic_query)
        reason = plan_query('What privacy rule applies and why?', self.project)
        self.assertIn('reason', reason.answer_facets)
        unrelated = plan_query('What telescope rule applies and why?', self.project)
        self.assertEqual(('telescope',), unrelated.candidate_queries)

    def test_reason_facets_remove_question_predicates_but_not_content_nouns(self):
        cases = [('Why preserve exact archive labels?', 'exact archive labels'),
                 ('Why does timestamp preservation matter here?', 'timestamp preservation'),
                 ('Why are the copper measurements required?', 'the copper measurements')]
        for query, subject in cases:
            with self.subTest(query=query):
                plan = plan_query(query, self.project)
                self.assertEqual(subject, plan.answer_subject)
                self.assertEqual(('reason',), plan.answer_facets)
                self.assertFalse(plan.generic_project_state)
                self.assertTrue(plan.topic_query)
        ambiguous = plan_query('Why privacy matters?', self.project)
        self.assertIsNone(ambiguous.answer_subject)
        self.assertEqual((), ambiguous.answer_facets)

    def test_normalized_soft_subject_does_not_reuse_or_weaken_hard_scope_anchors(self):
        plan = plan_query('Did the first telescope check for NEX-418 version 2.8.4 in local pass?', self.project)
        self.assertEqual('telescope', plan.answer_subject)
        self.assertEqual('earliest', plan.source_order)
        self.assertEqual(('`nex-418` 2.8.4',), plan.candidate_queries)
        self.assertEqual(('local',), plan.environments)
        self.assertTrue(plan.matches({'text': 'NEX-418 telescope version 2.8.4'}))
        for text in ('NEX-419 telescope 2.8.4', 'NEX-418 telescope 2.8.40'):
            self.assertFalse(plan.matches({'text': text}))
        literal = plan_query('Did the first `initial_rule` check pass?', self.project)
        self.assertEqual(('initial_rule',), literal.identifiers)
        self.assertEqual(('`initial_rule`',), literal.candidate_queries)
        self.assertFalse(literal.matches({'text': 'other_rule failed'}))
        self.assertIsNone(plan_query('What was known as-of version 2.8.4?', self.project).source_order)

    def test_scoped_execution_facets_preserve_exclusions_and_do_not_make_environment_a_topic(self):
        plan = plan_query('Was the earliest local copper test successful?', self.project)
        self.assertEqual('copper', plan.answer_subject)
        self.assertEqual(('local',), plan.environments)
        self.assertEqual('earliest', plan.source_order)
        negative = plan_query('Did the first check for NEX-418 version 2.8.4 excluding production pass?', self.project)
        self.assertEqual(('production',), negative.excluded_environments)
        self.assertFalse(negative.matches({'text': 'NEX-418 2.8.4', 'scope': {'environment': 'production'}}))
        for query in ('What originally governed NEX-418?',
                      'What was known as-of version 2.8.4?',
                      'What earlier results exist for NEX-418?'):
            self.assertTrue(plan_query(query, self.project).historical)
        for query in ('Where must original donor codes remain?',
                      'Is the original assistant report credible?'):
            self.assertFalse(plan_query(query, self.project).historical)

    def test_public_brief_keeps_deferred_output_without_requiring_question_words_in_the_source(self):
        import tempfile
        from codex_mem.config import configure
        from codex_mem.project_memory import ProjectMemory
        from codex_mem.project_memory_retrieval import build_project_brief
        from codex_mem.store import Store
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'proofs'
            project.mkdir()
            with Store(Path(directory) / 'data') as store:
                configure(store.data_dir, capture_scope='all', project_memory_mode='pilot',
                          project_memory_projects=[str(project)])
                row = store.remember(project, 'Revised proof request',
                    'Leave the paper proof out. Keep the web preview.',
                    source='hook:UserPromptSubmit', session_id='revision')
                memory = ProjectMemory(store)
                memory.commit(memory.claim(project), {'episodes': [], 'claims': [], 'relations': []})
                for xml in (build_project_brief(store, project,
                            query='Which requested output was deferred?')['xml'],
                            store.context(project, query='Which requested output was deferred?',
                                budget=6000, allow_remote=False, include_health=False)):
                    self.assertIn(row['body'], xml)
                unknown = build_project_brief(store, project,
                    query='Which requested iridescent telescope output was deferred?')
                self.assertNotIn(row['id'], unknown['xml'])
                self.assertNotIn(row['id'], build_project_brief(store, project,
                    query='Which requested output was deferred?', exclude_session_id='revision')['xml'])
                store.forget(project, [row['id']])
                self.assertNotIn(row['id'], build_project_brief(store, project,
                    query='Which requested output was deferred?')['xml'])

    def test_residual_compounds_cannot_collapse_into_an_empty_candidate_window(self):
        plan = plan_query('Did the first iridescent-telescope check pass?', self.project)
        self.assertEqual('iridescent telescope', plan.answer_subject)
        self.assertEqual(('iridescent telescope',), plan.candidate_queries)
        self.assertFalse(plan.generic_project_state)
        exact = plan_query('Did the first `iridescent-telescope` check pass?', self.project)
        self.assertEqual(('iridescent-telescope',), exact.identifiers)
        self.assertEqual(('`iridescent-telescope`',), exact.candidate_queries)


if __name__ == '__main__':
    unittest.main()
