"""General query planning uses fictional subjects unrelated to evaluation data."""
from pathlib import Path
import unittest

from codex_mem.project_memory_query import plan_query


class ProjectMemoryQueryTests(unittest.TestCase):
    project = Path('/tmp/query-planning-example')

    def test_natural_task_subject_is_not_every_word_in_the_question(self):
        plan = plan_query('Resume NEX-418 at our release checkpoint. Explain results and the approved rule the next edit must respect?', self.project)
        self.assertEqual(('`nex-418`',), plan.candidate_queries)
        self.assertTrue(plan.matches({'text': 'NEX-418 passed locally.'}))
        self.assertFalse(plan.matches({'text': 'NEX-419 passed locally.'}))
        self.assertFalse(plan.matches({'summary': 'MAP-112 is a separate legend task.',
                                       'rationale': 'It is different from NEX-418.'}))

    def test_comparison_is_a_union_of_positive_tasks_and_scope_projection(self):
        plan = plan_query('Compare NEX-418 and MAP-112 across local, staging and production. Keep volunteer-training separate.', self.project)
        self.assertEqual(('`nex-418`', '`map-112`'), plan.candidate_queries)
        self.assertEqual(('local', 'staging', 'production'), plan.environments)
        self.assertEqual('environments', plan.projection)
        self.assertNotIn('volunteer-training', plan.identifiers)
        self.assertTrue(plan.matches({'text': 'NEX-418 uses version 2.8.4 locally.'}))
        self.assertTrue(plan.matches({'text': 'MAP-112 is unverified in production.'}))
        self.assertFalse(plan.matches({'text': 'AUX-907 uses volunteer-training version 9.0.'}))

    def test_literal_and_explicit_code_constraints_stay_strict(self):
        literal = plan_query('NEX-418 MAP-112', self.project)
        self.assertFalse(literal.natural)
        self.assertFalse(literal.matches({'text': 'NEX-418 alone'}))
        self.assertTrue(literal.matches({'text': 'NEX-418 and MAP-112'}))
        code = plan_query('What does `bundle_rule` require for NEX-418 version 2.8.4?', self.project)
        self.assertTrue(code.matches({'text': 'NEX-418 bundle_rule version 2.8.4'}))
        self.assertFalse(code.matches({'text': 'NEX-418 bundle_rule version 2.8.40'}))
        self.assertFalse(code.matches({'text': 'NEX-418 other_rule version 2.8.4'}))
        self.assertIn('`bundle_rule`', code.candidate_queries[0])

    def test_version_comparison_does_not_require_both_versions_in_each_record(self):
        plan = plan_query('Compare NEX-418 versions 2.8.4 and 2.7.1.', self.project)
        self.assertEqual(2, len(plan.candidate_queries))
        self.assertTrue(plan.matches({'text': 'NEX-418 version 2.8.4 locally'}))
        self.assertTrue(plan.matches({'text': 'NEX-418 version 2.7.1 staging'}))
        self.assertFalse(plan.matches({'text': 'NEX-418 version 2.8.40'}))

    def test_history_and_comparison_are_distinct_and_negative_task_does_not_become_positive(self):
        self.assertTrue(plan_query('What originally governed NEX-418?', self.project).historical)
        self.assertFalse(plan_query('Compare earlier NEX-418 failures with current results.', self.project).historical)
        plan = plan_query('Explain NEX-418, excluding MAP-112.', self.project)
        self.assertEqual(('nex-418',), plan.task_ids)
        self.assertFalse(plan.matches({'text': 'MAP-112 unrelated result'}))

    def test_continuity_projection_requests_bounded_supported_history(self):
        plan = plan_query('Which sessions explicitly continue NEX-418? Explain the link using recorded user statements.', self.project)
        self.assertEqual('continuity', plan.projection)
        self.assertEqual(('`nex-418`',), plan.candidate_queries)

    def test_negative_relation_stops_at_clause_boundary_and_history_comparison_keeps_history(self):
        plan = plan_query('Compare NEX-418, not MAP-112, with AUX-907.', self.project)
        self.assertEqual(('nex-418', 'aux-907'), plan.task_ids)
        unrelated = plan_query('Resume NEX-418, unrelated to MAP-112.', self.project)
        self.assertEqual(('nex-418',), unrelated.task_ids)
        history = plan_query('Compare the original NEX-418 history with its current state.', self.project)
        self.assertFalse(history.historical)
        self.assertEqual('history', history.projection)

    def test_negative_spans_apply_to_versions_and_environment_scopes(self):
        version = plan_query('Show NEX-418 version 2.8.4, not 2.7.1.', self.project)
        self.assertEqual(('2.8.4',), version.versions)
        self.assertFalse(version.matches({'text': 'NEX-418 version 2.7.1'}))
        excluded_only = plan_query('Show NEX-418, not version 2.7.1.', self.project)
        self.assertFalse(excluded_only.matches({'text': 'NEX-418 version 2.7.1'}))
        scope = plan_query('Compare NEX-418 and MAP-112 in production, excluding staging.', self.project)
        self.assertEqual(('production',), scope.environments)
        positive = plan_query('Compare NEX-418 not MAP-112 with AUX-907.', self.project)
        self.assertEqual(('nex-418', 'aux-907'), positive.task_ids)

    def test_literal_version_conjunction_and_negative_only_scope_remain_strict(self):
        literal = plan_query('NEX-418 2.8.4 2.7.1', self.project)
        self.assertFalse(literal.matches({'text': 'NEX-418 version 2.8.4'}))
        self.assertTrue(literal.matches({'text': 'NEX-418 versions 2.8.4 and 2.7.1'}))
        negative = plan_query('Show NEX-418, excluding staging.', self.project)
        self.assertEqual(('staging',), negative.excluded_environments)
        self.assertFalse(negative.matches({'text': 'NEX-418 version 2.7.1', 'scope': {'environment': 'staging'}}))

    def test_subject_free_outcome_questions_use_explicit_bounded_project_scope(self):
        for query in ('What delivery is supported?', 'Which results are confirmed?',
                      'What has been delivered?', 'What work is complete?'):
            with self.subTest(query=query):
                plan = plan_query(query, self.project)
                self.assertTrue(plan.generic_project_state)
                self.assertEqual(('',), plan.candidate_queries)
                self.assertFalse(plan.topic_query)
        for query in ('What delivery schedule is supported?', 'Which cobalt results are confirmed?',
                      'What has been delivered for the telescope?', 'How does telescope coating behave?'):
            with self.subTest(query=query):
                plan = plan_query(query, self.project)
                self.assertFalse(plan.generic_project_state)
                self.assertNotEqual(('',), plan.candidate_queries)
        scoped = plan_query('Which results are confirmed for NEX-418 version 2.8.4?', self.project)
        self.assertFalse(scoped.matches({'text': 'NEX-418 version 2.8.40'}))

    def test_latest_request_relation_is_generic_continuity_without_a_topical_subject(self):
        for query in ('How does the last request relate to prior work?',
                      'How is the latest instruction connected to earlier tasks?',
                      'How does the latest request compare with earlier work?',
                      'Is the most recent task a continuation of existing work or a separate task?'):
            with self.subTest(query=query):
                plan = plan_query(query, self.project)
                self.assertTrue(plan.generic_project_state)
                self.assertTrue(plan.continuation)
                self.assertEqual('continuity', plan.projection)
                self.assertEqual(('',), plan.candidate_queries)
                self.assertFalse(plan.topic_query)
        topical = plan_query('How does the last telescope request relate to prior work?', self.project)
        self.assertFalse(topical.generic_project_state)
        self.assertNotEqual(('',), topical.candidate_queries)

    def test_complete_role_attribution_frames_keep_residual_subjects_strict(self):
        for query in ('What is the standing of the assistant prediction?',
                      "What was the support for the assistant's assessment?"):
            self.assertTrue(plan_query(query, self.project).generic_project_state)
        for query in ('What is the status of the assistant report about telescope coating?',
                      'What is the status of the assistant estimate for QA-42 version 2.8.4?'):
            self.assertFalse(plan_query(query, self.project).generic_project_state)
        exact = plan_query('What is the status of the assistant estimate for QA-42 version 2.8.4?', self.project)
        self.assertFalse(exact.matches({'text': 'QA-42 version 2.8.40'}))

    def test_rationale_and_inflected_russian_environment_queries_select_fields_not_domain_words(self):
        rationale = plan_query('Why was the editable vector format chosen and when should it be reconsidered?', self.project)
        self.assertEqual('rationale', rationale.projection)
        russian = plan_query('Раздели состояние генератора по локальной среде, стейджингу и продакшену. Что проверено?', self.project)
        self.assertEqual(('local', 'staging', 'production'), russian.environments)
        self.assertEqual('environments', russian.projection)
