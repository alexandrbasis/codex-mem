"""General query planning uses fictional subjects unrelated to evaluation data."""
from pathlib import Path
import tempfile
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

    def test_comparison_inflections_do_not_turn_earlier_evidence_into_a_history_only_lookup(self):
        for wording in ('Compare', 'Compared with', 'Comparing', 'A comparison of',
                        'How it compares with', 'Two comparisons of'):
            with self.subTest(wording=wording):
                plan = plan_query(wording + ' earlier NEX-418 results with current results?', self.project)
                self.assertFalse(plan.historical)
                self.assertEqual('history', plan.projection)
                self.assertEqual(('`nex-418`',), plan.candidate_queries)
                self.assertFalse(plan.matches({'text': 'MAP-112 earlier results'}))
        for query in ('What earlier results exist for NEX-418?',
                      'What was the state of NEX-418 as-of version 2.8.4?',
                      'What originally governed NEX-418?'):
            with self.subTest(query=query):
                self.assertTrue(plan_query(query, self.project).historical)

    def test_comparison_inflections_preserve_exact_and_excluded_scope(self):
        plan = plan_query('How do NEX-418 results for `bundle_rule` version 2.8.4 in production '
                          'look compared with earlier evidence, excluding staging?', self.project)
        self.assertFalse(plan.historical)
        self.assertEqual(('production',), plan.environments)
        self.assertEqual(('staging',), plan.excluded_environments)
        self.assertTrue(plan.matches({'text': 'NEX-418 bundle_rule 2.8.4',
                                      'scope': {'environment': 'production'}}))
        for text in ('NEX-419 bundle_rule 2.8.4', 'NEX-418 other_rule 2.8.4',
                     'NEX-418 bundle_rule 2.8.40'):
            self.assertFalse(plan.matches({'text': text}))
        self.assertFalse(plan.matches({'text': 'NEX-418 bundle_rule 2.8.4',
                                       'scope': {'environment': 'staging'}}))

    def test_compared_with_reads_attributed_execution_through_the_public_brief(self):
        from codex_mem.project_memory import ProjectMemory
        from codex_mem.project_memory_retrieval import build_project_brief
        from codex_mem.store import Store
        from codex_mem.config import configure

        with tempfile.TemporaryDirectory() as directory:
            project = str(Path(directory, 'project').resolve())
            with Store(Path(directory, 'data')) as store:
                configure(store.data_dir, project_memory_mode='pilot',
                          project_memory_projects=[project], capture_scope='all')
                observed = 'NEX-418 local check passed. Deployment was not attempted.'
                report = 'NEX-418 deployment is finished.'
                tool = store.remember(project, 'Execution', observed,
                                      source='hook:PostToolUse', session_id='trial')
                assistant = store.remember(project, 'Report', report,
                                           source='hook:Stop', session_id='trial')
                memory = ProjectMemory(store)
                job = memory.claim(project)
                refs = [{'source_id': source['id'], 'start': 0, 'end': len(text), 'quote': text}
                        for source, text in ((tool, observed), (assistant, report))]
                memory.commit(job, {'episodes': [{'key': 'trial', 'summary': observed,
                    'outcome': observed + ' ' + report,
                    'evidence': {'summary': [refs[0]], 'outcome': refs}}]})
                brief = build_project_brief(store, project,
                    query='What does observed execution establish for NEX-418, compared with the earlier report?',
                    budget=6000)
                self.assertNotEqual('historical_query', brief.get('skip_reason'))
                self.assertIn(observed, brief['xml'])
                self.assertIn(report, brief['xml'])
                self.assertIn('source_role="tool_record"', brief['xml'])
                self.assertIn('source_role="assistant_report"', brief['xml'])

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

    def test_compositional_evidence_questions_are_project_facets_only_without_subjects(self):
        for query in ('What output does the evidence establish?', 'What delivery does the record support?',
                      'Which completed evidence can be retained?', 'What does the current evidence show?',
                      'What evidence supports the delivery?', 'What does the evidence establish about completion?'):
            with self.subTest(query=query):
                plan = plan_query(query, self.project)
                self.assertTrue(plan.generic_project_state)
                self.assertEqual(('',), plan.candidate_queries)
                self.assertFalse(plan.topic_query)
        for query in ('What telescope output does the evidence establish?',
                      'What delivery does the cobalt monitor support?',
                      'Which completed telescope evidence can be retained?'):
            self.assertFalse(plan_query(query, self.project).generic_project_state)
        environment = plan_query('What output does the evidence establish in production?', self.project)
        self.assertTrue(environment.generic_project_state)
        self.assertEqual(('production',), environment.environments)
        scoped = plan_query('What output does the evidence establish for QA-42 version 2.8.4?', self.project)
        self.assertFalse(scoped.matches({'text': 'QA-42 version 2.8.40'}))

    def test_assessing_earlier_report_against_current_evidence_is_not_as_of_history(self):
        for query in ('Does the earlier assistant assessment remain credible?',
                      'Was the previous assistant completion claim accurate?',
                      'How does the original assistant report compare with current evidence?',
                      'Does the earlier assistant assessment still hold up?'):
            with self.subTest(query=query):
                plan = plan_query(query, self.project)
                self.assertFalse(plan.historical)
                self.assertTrue(plan.assesses_current_evidence)
                self.assertTrue(plan.generic_project_state)
        topical = plan_query('Did the earlier assistant statement establish both alignment charts existed?', self.project)
        self.assertFalse(topical.historical)
        self.assertTrue(topical.assesses_current_evidence)
        self.assertFalse(topical.generic_project_state)
        self.assertIn('alignment', topical.candidate_queries[0])
        unknown = plan_query('Was the earlier assistant estimate about cobalt mirrors accurate?', self.project)
        self.assertFalse(unknown.generic_project_state)
        self.assertIn('cobalt', unknown.candidate_queries[0])
        for query in ('What originally governed QA-42?',
                      'What did the assistant report say as of 2031-02-03?'):
            self.assertTrue(plan_query(query, self.project).historical)

    def test_rationale_and_inflected_russian_environment_queries_select_fields_not_domain_words(self):
        rationale = plan_query('Why was the editable vector format chosen and when should it be reconsidered?', self.project)
        self.assertEqual('rationale', rationale.projection)
        russian = plan_query('Раздели состояние генератора по локальной среде, стейджингу и продакшену. Что проверено?', self.project)
        self.assertEqual(('local', 'staging', 'production'), russian.environments)
        self.assertEqual('environments', russian.projection)
