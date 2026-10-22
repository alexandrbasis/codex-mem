"""Original source visibility for grammatical state questions, without new truth."""
from xml.etree import ElementTree as ET
import unittest

from codex_mem.project_memory import ProjectMemory
from codex_mem.project_memory_query import plan_query
from codex_mem.project_memory_retrieval import build_project_brief
import test_project_memory_source_coverage_allocation as support


class RetrospectiveSourceCoverageTests(unittest.TestCase):
    setUp = support.SourceCoverageAllocationTests.setUp
    user = support.SourceCoverageAllocationTests.user
    consume = support.SourceCoverageAllocationTests.consume
    span = staticmethod(support.SourceCoverageAllocationTests.span)
    visible = staticmethod(support.SourceCoverageAllocationTests.visible)
    tool = support.SourceCoverageAllocationTests.tool

    def test_subject_free_revision_question_keeps_original_revision(self):
        original = self.user('Keep sensor measurements and create an illustrated summary.', 'initial')
        self.consume(original)
        changed = self.user('Continue the same sensor study. Drop the illustrated summary. Retain raw units.', 'revision')
        self.consume(changed)
        query = 'Which requested addition did I withdraw?'
        brief = build_project_brief(self.store, self.project, query=query, budget=6000)
        self.assertTrue(self.visible(brief['xml'], changed), brief['xml'])
        self.assertEqual(plan_query(query, self.project).answer_subject, '')
        self.assertIn('current_revision', plan_query(query, self.project).answer_facets)
        self.assertLessEqual(len(brief['xml']), 6000)
        unknown = build_project_brief(self.store, self.project,
                                     query='Which telescope addition did I withdraw?', budget=6000)
        self.assertNotIn(changed['id'], unknown['xml'])

    def test_named_completion_keeps_separate_unassociated_assistant_report(self):
        request = self.user('Keep receiver calibration available during the update.', 'request')
        self.consume(request)
        observed = self.tool('Receiver audit: package staged. Live update was not attempted.', 1)
        memory = ProjectMemory(self.store)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        report = self.store.remember(self.project, 'Assistant report', 'Everything is finished and installed.',
                                     source='hook:Stop', session_id='assistant')
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        brief = build_project_brief(self.store, self.project,
                                  query='Has the receiver update actually been demonstrated?', budget=6000)
        self.assertIn(observed['id'], brief['xml'])
        self.assertTrue(self.visible(brief['xml'], report), brief['xml'])
        wrappers = [item for item in ET.fromstring(brief['xml']).findall('item')
                    if any(ref.get('source_id') == report['id'] for ref in item.iter('source'))]
        self.assertTrue(all(item.get('relationship_to_query') == 'unresolved' for item in wrappers))
        self.assertTrue(all(item.get('source_role') == 'assistant_report' for item in wrappers))
        unknown = build_project_brief(self.store, self.project,
                                     query='Has the telescope update actually been demonstrated?', budget=6000)
        self.assertNotIn(report['id'], unknown['xml'])

    def test_deictic_reason_and_coordinated_state_keep_qualified_originals(self):
        reason = self.user('Keep the calibration sheet separate. Combining sheets would mix instrument owners.', 'reason')
        self.consume(reason)
        observed = self.tool('Sheet audit: labels matched. Remote write skipped.', 2)
        memory = ProjectMemory(self.store)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        pending = self.user('Ask the operator to approve the sample before any remote write. Both remain pending.', 'pending')
        self.consume(pending)
        reason_view = build_project_brief(self.store, self.project,
                                         query='Why was that grouping requested?', budget=6000)
        self.assertTrue(self.visible(reason_view['xml'], reason), reason_view['xml'])
        state = build_project_brief(self.store, self.project,
            query='Given the check and my latest message, what is ready and what remains?', budget=6000)
        self.assertIn(observed['id'], state['xml'])
        self.assertTrue(self.visible(state['xml'], pending), state['xml'])
        self.assertLessEqual(len(state['xml']), 6000)

    def test_active_receipt_assessment_keeps_roles_and_named_object(self):
        request = self.user('The monitor bundle needs a signed receipt.', 'request')
        self.consume(request)
        observed = self.tool('Monitor audit: bundle delivery disabled.', 3)
        memory = ProjectMemory(self.store)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        report = self.store.remember(self.project, 'Assistant report', 'The monitor received the final bundle.',
                                     source='hook:Stop', session_id='assistant')
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        query = 'Has the monitor actually received the finished bundle?'
        plan = plan_query(query, self.project)
        self.assertIn('execution_assessment', plan.answer_facets)
        self.assertIn('monitor', plan.answer_subject)
        self.assertIn('bundle', plan.answer_subject)
        brief = build_project_brief(self.store, self.project, query=query, budget=6000)
        self.assertTrue(self.visible(brief['xml'], report), brief['xml'])
        self.assertIn(observed['id'], brief['xml'])
        unknown = build_project_brief(self.store, self.project,
                                     query='Has the telescope actually received the finished bundle?', budget=6000)
        self.assertNotIn(report['id'], unknown['xml'])

    def test_original_role_context_keeps_hard_guards_and_whole_source_limits(self):
        request = self.user('ORB-27 calibration must remain local.', 'request')
        self.consume(request)
        memory = ProjectMemory(self.store)
        blocked = []
        for body, session, project in (
            ('ORB-28 calibration is finished.', 'other-task', self.project),
            ('ORB-27 production calibration finished.', 'wrong-environment', self.project),
            ('ORB-27 calibration finished.', 'hidden-session', self.project),
            ('ORB-27 calibration details. ' * 80, 'large-report', self.project),
            ('ORB-27 calibration finished elsewhere.', 'foreign', self.other),
        ):
            row = self.store.remember(project, 'Assistant report', body,
                                      source='hook:Stop', session_id=session)
            blocked.append(row)
            owner = ProjectMemory(self.store)
            claimed = owner.claim(project)
            owner.commit(claimed, {'episodes': [], 'claims': [], 'relations': []})
        query = 'Has ORB-27 calibration been demonstrated in local?'
        brief = build_project_brief(self.store, self.project, query=query, budget=6000,
                                  exclude_session_id='hidden-session')
        self.assertTrue(self.visible(brief['xml'], request))
        for row in blocked:
            self.assertNotIn(row['id'], brief['xml'])
        self.assertLessEqual(len(brief['xml']), 6000)

    def test_second_render_is_identical_and_counters_match_delivered_sources(self):
        from codex_mem.project_memory_retrieval import render_project_brief
        original = self.user('The telescope export needs review.', 'initial')
        self.consume(original)
        memory = ProjectMemory(self.store)
        for index in range(3):
            self.user(f'Keep the sample number {index} until approval.', f'followup-{index}')
            memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        for budget in (600, 1400, 2900, 6000):
            brief = build_project_brief(self.store, self.project,
                                      query='Which telescope export remains required?', budget=budget)
            delivered = render_project_brief(brief, budget=budget)
            self.assertEqual(brief['xml'], delivered)
            self.assertEqual(delivered, render_project_brief(brief, budget=budget))
            tree = ET.fromstring(delivered)
            actual = sum(len(list(item.iter('source'))) for item in tree.findall('item')
                         if item.get('selection_basis') == 'unassociated_user_context')
            coverage = brief['unassociated_user_context_coverage']
            self.assertEqual(coverage['selected_source_count'], actual)
            self.assertEqual(coverage['omitted'], coverage['eligible'] - actual)
            for marker in tree.findall('source-context-coverage'):
                self.assertEqual(int(marker.get('selected_source_count')), actual)
                self.assertEqual(int(marker.get('omitted')), coverage['omitted'])
            self.assertLessEqual(len(delivered), budget)

    def test_unknown_soft_subject_gets_only_neutral_original_observations(self):
        original = self.user('Preserve measurement worksheets.', 'initial')
        self.consume(original, claim=True)
        latest = self.user('The instrument labels need operator approval.', 'latest')
        self.consume(latest)
        before = ProjectMemory(self.store).snapshot(self.project)
        brief = build_project_brief(self.store, self.project,
                                  query='Why was the telescope restriction requested?', budget=6000)
        self.assertTrue(self.visible(brief['xml'], original), brief['xml'])
        self.assertTrue(self.visible(brief['xml'], latest), brief['xml'])
        tree = ET.fromstring(brief['xml'])
        for item in tree.findall('item'):
            if list(item.iter('source')):
                self.assertEqual(item.get('relationship_to_query'), 'unresolved')
                self.assertEqual(item.get('is_requirement_claim'), 'False')
                self.assertIsNone(item.get('workflow_state'))
        self.assertEqual(ProjectMemory(self.store).snapshot(self.project)['claims'], before['claims'])
        hard_unknown = build_project_brief(self.store, self.project,
                                          query='Why was TASK-99 requested?', budget=6000)
        self.assertNotIn(original['id'], hard_unknown['xml'])
        self.assertNotIn(latest['id'], hard_unknown['xml'])
