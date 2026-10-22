"""Selected source originals compete across render lanes, without new truth."""
from xml.etree import ElementTree as ET
import unittest

from codex_mem.project_memory import ProjectMemory
from codex_mem.project_memory_retrieval import build_project_brief, render_project_brief
import test_project_memory_source_coverage_allocation as support


class CrosslaneSourceRenderTests(unittest.TestCase):
    setUp = support.SourceCoverageAllocationTests.setUp
    user = support.SourceCoverageAllocationTests.user
    consume = support.SourceCoverageAllocationTests.consume
    span = staticmethod(support.SourceCoverageAllocationTests.span)
    visible = staticmethod(support.SourceCoverageAllocationTests.visible)
    tool = support.SourceCoverageAllocationTests.tool

    def test_selected_revision_and_approval_precede_older_neutral_request(self):
        memory = ProjectMemory(self.store)
        original = self.user('Exercises need a caption option and a closing bell. Some learners cannot hear the bell.', 'initial')
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        tools = []
        for index in range(2):
            tools.append(self.tool('Lesson bundle fixture: captions match. Remote delivery disabled.', index))
            memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        report = self.store.remember(self.project, 'Assistant report', 'The facilitator received the finished lesson bundle.',
                                     source='hook:Stop', session_id='report')
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        revision = self.user('Facilitator lesson bundle: remove the closing bell; retain captions and the optional visual cue.', 'revision')
        self.consume(revision)
        approval = self.user('Lesson bundle: ask the facilitator to approve the caption sample before delivery. Approval and delivery remain due.', 'approval')
        self.consume(approval, claim=True)
        brief = build_project_brief(self.store, self.project,
            query='Has the facilitator actually received the finished lesson bundle?', budget=6000)
        selected = {ref['source_id'] for record in brief['records'] for field in record.get('fields', [])
                    for ref in field.get('evidence', [])}
        self.assertIn(revision['id'], selected)
        self.assertIn(approval['id'], selected)
        for source in (revision, approval, original, report):
            self.assertTrue(self.visible(brief['xml'], source), (source['body'], brief['xml']))
        wrappers = [item for item in ET.fromstring(brief['xml']).findall('item')
                    if item.find('execution') is not None]
        self.assertTrue(wrappers, brief['xml'])
        self.assertTrue(all({'tool_input.cmd', 'tool_response.output'} <= {
            ref.get('source_field') for ref in item.iter('source')} for item in wrappers))
        tree = ET.fromstring(brief['xml'])
        for source in (revision, approval, original, report):
            quotations = [ref.findtext('quote_excerpt') for ref in tree.iter('source')
                          if ref.get('source_id') == source['id'] and ref.find('quote_excerpt') is not None]
            self.assertEqual(quotations, [source['body']], source['body'])
        self.assertTrue(all(item.find('execution').get('exit_code') == '0' for item in wrappers))
        self.assertEqual(brief['xml'], render_project_brief(brief, budget=6000))
        self.assertLessEqual(len(brief['xml']), 6000)

    def test_distinct_scope_and_history_associations_keep_their_own_qualifiers(self):
        source = self.user('Keep the independent measurement worksheet.', 'original')
        self.consume(source)
        pointer = {'source_id': source['id'], 'source_role': 'user_intent',
                   'start': 0, 'end': len(source['body']), 'quote_excerpt': source['body'],
                   'quote_truncated': False}
        declarations = [('unknown-history', {}, False, 'historical_applicability_unknown'),
                        ('local-current', {'environment': 'local', 'version': '1.2.0'}, True, None),
                        ('production-history', {'environment': 'production', 'version': '2.0.0'}, False,
                         'historical_applicability_unknown')]
        for ordered in (declarations, list(reversed(declarations))):
            with self.subTest(order=[item[0] for item in ordered]):
                records = [{'id': name, 'lane': 'episode', 'scope': scope,
                            'episode_version': index + 1, 'is_current_head': current,
                            'uncertainty': uncertainty, 'fields': [{'name': 'rationale', 'text': '',
                            'basis': 'attributed_source_excerpts', 'field_association': 'model_inference',
                            'evidence': [dict(pointer)]}]}
                           for index, (name, scope, current, uncertainty) in enumerate(ordered)]
                brief = {'records': records, 'coverage': {}, 'user_source_coverage_requested': True}
                xml = render_project_brief(brief, budget=6000)
                tree = ET.fromstring(xml)
                for name, scope, current, uncertainty in declarations:
                    item = tree.find("item[@id='" + name + "']")
                    self.assertIsNotNone(item, xml)
                    self.assertEqual(item.get('environment'), scope.get('environment'))
                    self.assertEqual(item.get('version'), scope.get('version'))
                    self.assertEqual(item.get('is_current_head'), str(current))
                    self.assertEqual(item.get('uncertainty'), uncertainty)
                quotes = [ref.findtext('quote_excerpt') for ref in tree.iter('source')
                          if ref.get('source_id') == source['id'] and ref.find('quote_excerpt') is not None]
                self.assertEqual(quotes, [source['body']])
                self.assertEqual(xml, render_project_brief(brief, budget=6000))
                self.assertLessEqual(len(xml), 6000)

    def test_unrelated_supplement_cannot_displace_selected_originals_and_large_source_is_whole_omitted(self):
        revision = self.user('The telescope bundle must omit the bell and retain the visual cue.', 'revision')
        self.consume(revision)
        approval = self.user('The telescope bundle needs operator approval before delivery.', 'approval')
        self.consume(approval)
        unrelated = self.user('A different independent activity needs a padded shipping box.', 'unrelated')
        memory = ProjectMemory(self.store)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        large = self.user('Unrelated shipping details. ' * 30, 'large')
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        for budget in (600, 1400, 2900, 6000):
            with self.subTest(budget=budget):
                brief = build_project_brief(self.store, self.project,
                    query='Which requested telescope output is deferred?', budget=budget)
                xml = render_project_brief(brief, budget=budget)
                self.assertEqual(brief['xml'], xml)
                self.assertNotIn(large['id'], xml)
                self.assertLessEqual(len(xml), budget)
                if budget == 6000:
                    self.assertTrue(self.visible(xml, revision), xml)
                    self.assertTrue(self.visible(xml, approval), xml)
                tree = ET.fromstring(xml)
                wrappers = [item for item in tree.findall('item') if any(
                    ref.get('source_id') == unrelated['id'] for ref in item.iter('source'))]
                for item in wrappers:
                    self.assertEqual(item.get('relationship_to_query'), 'unresolved')
                    self.assertEqual(item.get('is_requirement_claim'), 'False')
                    self.assertIsNone(item.get('workflow_state'))
                selected = sum(len(list(item.iter('source'))) for item in tree.findall('item')
                               if item.get('selection_basis') == 'unassociated_user_context')
                counts = brief['unassociated_user_context_coverage']
                self.assertEqual(counts['selected_source_count'], selected)
                self.assertEqual(counts['omitted'], counts['eligible'] - selected)
                if budget == 600:
                    self.assertIsNotNone(tree.find('omitted'), xml)


if __name__ == '__main__':
    unittest.main()
