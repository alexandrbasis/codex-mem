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

    def test_rendered_role_original_does_not_reserve_its_duplicate_before_complete_selected_execution(self):
        memory = ProjectMemory(self.store)
        first = self.user('Packing station crate labels must stay unchanged. Add a scan sheet because offline staff need printed identifiers.', 'tools')
        earlier = self.tool('Scanner preview: sample rows loaded; shipping disabled.', 20)
        old_ref = self.span(earlier)
        memory.commit(memory.claim(self.project), {'episodes': [
            {'key': 'preview', 'summary': earlier['body'], 'outcome': earlier['body'], 'goal': first['body'],
             'evidence': {'summary': [old_ref], 'outcome': [old_ref], 'goal': [self.span(first)]}}], 'claims': [], 'relations': []})
        self.store.remember(self.project, 'Assistant report', 'Everything is ready and complete.',
                            source='hook:Stop', session_id='report')
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        revision = self.user('Resume packing station. Drop the scan sheet, but preserve the crate labels and all original station identifiers.', 'revision')
        self.consume(revision)
        output = 'Local scanner pass: sample row counts matched. Crate labels were not inspected.'
        observed = self.tool(output, 21)
        prefix = output[:output.index(' Crate')]
        ref = {'source_id': observed['id'], 'start': 0, 'end': len(prefix), 'quote': prefix}
        result = memory.commit(memory.claim(self.project), {'episodes': [
            {'key': 'pass', 'summary': prefix, 'outcome': prefix,
             'evidence': {'summary': [ref], 'outcome': [ref]}}], 'claims': [], 'relations': []})
        self.assertEqual(result['status'], 'committed')
        pending = self.user('The station operator must approve the crate-label sample before shipping. Both remain pending until that independent approval is recorded.', 'pending')
        self.consume(pending)
        brief = build_project_brief(self.store, self.project,
            query='Have crate labels been verified by the local pass?', budget=6000)
        tree = ET.fromstring(brief['xml'])
        complete = [node for node in tree.iter('source') if node.get('source_id') == observed['id']
                    and node.findtext('quote_excerpt') == output]
        self.assertTrue(complete, brief['xml'])
        self.assertEqual(brief['xml'], render_project_brief(brief, budget=6000))
        self.assertLessEqual(len(brief['xml']), 6000)

    def test_individually_fitting_tools_cannot_reserve_more_than_each_project_budget(self):
        memory = ProjectMemory(self.store)
        revision = self.user('ORB-27: keep original instrument units. Omit the optional paper chart.', 'revision')
        self.consume(revision)
        for index in range(2):
            self.tool('ORB-27 instrument check: sample units preserved. Shipping was not attempted.', 30 + index)
            memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        brief = build_project_brief(self.store, self.project,
            query='What requirements apply to ORB-27?', budget=2900,
            selected_project_scopes=[str(self.project), str(self.other)])
        self.assertTrue(self.visible(brief['xml'], revision), brief['xml'])
        self.assertTrue(ET.fromstring(brief['xml']).findall('.//execution'), brief['xml'])
        self.assertEqual(brief['xml'], render_project_brief(brief, budget=2900))
        self.assertLessEqual(len(brief['xml']), 2900)

    def test_exact_source_sequence_beats_multi_source_episode_range_for_transport_order(self):
        older = self.user('Keep the original option. ' + 'Review its independent rationale. ' * 4, 'older')
        newer = self.user('Use the revised option. ' + 'Keep the later approval prerequisite. ' * 4, 'newer')
        def pointer(source, seq=None):
            return {'source_id': source['id'], 'source_role': 'user_intent', 'start': 0,
                    'end': len(source['body']), 'quote_excerpt': source['body'], 'quote_truncated': False,
                    **({'source_capture_seq': seq} if seq is not None else {})}
        def field(ref):
            return {'name': 'source_custody', 'text': '', 'basis': 'attributed_source_excerpts', 'evidence': [ref]}
        records = [
            {'id': 'multi-source-summary', 'lane': 'episode', 'source_capture_seq_min': 1,
             'source_capture_seq_max': 20, 'fields': [field(pointer(older))]},
            {'id': 'old-custody', 'lane': 'intent_custody', 'source_role': 'user_intent',
             'disposition': 'unresolved', 'is_requirement_claim': False, 'fields': [field(pointer(older, 1))]},
            {'id': 'new-custody', 'lane': 'intent_custody', 'source_role': 'user_intent',
             'disposition': 'unresolved', 'is_requirement_claim': False, 'fields': [field(pointer(newer, 10))]}]
        brief = {'records': records, 'coverage': {}, 'user_source_coverage_requested': True}
        xml = render_project_brief(brief, budget=1000)
        self.assertTrue(self.visible(xml, newer), xml)
        self.assertLessEqual(len(xml), 1000)

    def test_neutral_withdrawal_gets_a_turn_before_secondary_preview_beside_visible_old_request(self):
        memory = ProjectMemory(self.store)
        original = self.user('For the station queue never remove entries only because they are old. Add a waiting-time diagram. Some entries wait for spare parts. A waiting period alone cannot establish abandonment.', 'original')
        self.consume(original)
        self.tool('Queue preview: age-only removals disabled; waiting-time diagram drawn. Shared writes: zero.', 41)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        changed = self.user('Continue this activity. Withdraw the diagram only. Keep the rule against age-only removals. Do not create a different task.', 'changed')
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        self.tool('Station queue check: no age-only removals. Shared queue writes: zero.', 42)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        report = self.store.remember(self.project, 'Assistant report', 'The station queue changes are finished.',
                                     source='hook:Stop', session_id='report')
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        approval = self.user('The queue operator must approve the proposed changes before any shared queue write. Both remain pending; no shared update has happened.', 'approval')
        self.consume(approval)
        brief = build_project_brief(self.store, self.project,
            query='Does the assistant completion report prove a shared queue write?', budget=6000)
        self.assertTrue(any(ref['source_id'] == changed['id'] for record in brief['unassociated_user_context']
                            for field in record['fields'] for ref in field['evidence']))
        for source in (changed, original, approval, report):
            self.assertTrue(self.visible(brief['xml'], source), (source['body'], brief['xml']))
        tree = ET.fromstring(brief['xml'])
        self.assertTrue(tree.findall('.//execution'), brief['xml'])
        for item in tree.findall('item'):
            if any(ref.get('source_id') == changed['id'] for ref in item.iter('source')):
                self.assertEqual(item.get('relationship_to_query'), 'unresolved')
                self.assertEqual(item.get('is_requirement_claim'), 'False')
                self.assertIsNone(item.get('workflow_state'))
            if item.find('execution') is not None:
                fields = {ref.get('source_field') for ref in item.iter('source')}
                self.assertTrue({'tool_input.cmd', 'tool_response.output'} <= fields)
                self.assertEqual(item.find('execution').get('exit_code'), '0')
        self.assertEqual(brief['xml'], render_project_brief(brief, budget=6000))
        self.assertLessEqual(len(brief['xml']), 6000)

    def test_policy_comparison_keeps_original_policy_and_revision_before_unrequested_reports(self):
        memory = ProjectMemory(self.store)
        policy = self.user('The telescope export must preserve instrument units. Add a printable table because reviewers work offline.', 'policy')
        self.consume(policy)
        self.tool('Telescope fixture: units retained; printable table rendered. Device writes: zero.', 51)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        revision = self.user('Continue the telescope export. Omit the printable table, but preserve instrument units.', 'revision')
        self.consume(revision)
        self.tool('Telescope check: instrument units retained. Live delivery was not attempted.', 52)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        self.store.remember(self.project, 'Assistant report', 'The telescope export is finished.',
                            source='hook:Stop', session_id='report')
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        other = self.user('A separate microscope task can use converted units. This does not change telescope export.', 'other', project=self.other)
        memory.commit(memory.claim(self.other), {'episodes': [], 'claims': [], 'relations': []})
        query = 'Does the microscope exception change the telescope unit requirement?'
        scopes = [str(self.project), str(self.other)]
        parts = [build_project_brief(self.store, project, query=query, budget=2900,
                                    selected_project_scopes=scopes) for project in (self.project, self.other)]
        self.assertFalse(parts[0]['source_evidence_requested'])
        self.assertTrue(parts[0]['user_source_coverage_requested'])
        self.assertTrue(self.visible(parts[0]['xml'], policy), parts[0]['xml'])
        self.assertTrue(self.visible(parts[0]['xml'], revision), parts[0]['xml'])
        self.assertTrue(self.visible(parts[1]['xml'], other), parts[1]['xml'])
        self.assertLessEqual(sum(len(part['xml']) for part in parts), 5800)
        for part in parts:
            self.assertEqual(part['xml'], render_project_brief(part, budget=2900))

    def test_unknown_mixed_question_keeps_primary_execution_and_report_with_user_rules(self):
        memory = ProjectMemory(self.store)
        sources = []
        for index, text in enumerate((
            'The rehearsal must preserve local event numbers. An optional paper chart may accompany the sample. Reviewers compare the original numbers with their handwritten notes.',
            'Continue the rehearsal. Remove the paper chart but preserve all event numbers. Do not infer that an older number denotes a finished or abandoned event.',
            'Keep the rehearsal examples separate for each participant. A shared example would obscure whose observations the printed notes describe.',
        )):
            text += ' Keep each rehearsal observation separate for independent review.' * 3
            row = self.user(text, f'rule-{index}')
            sources.append(row)
            self.consume(row)
        self.tool('Rehearsal preview: event numbers retained; paper chart drawn. Device writes: zero.', 81)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        actual = self.tool('Rehearsal fixture: event numbers retained. Remote delivery was not attempted.', 82)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        report = self.store.remember(self.project, 'Assistant report', 'Everything is finished and delivered.',
                                     source='hook:Stop', session_id='report')
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        approval = self.user('The rehearsal facilitator must approve the number sample before delivery. Approval and delivery remain pending for the next person.', 'approval')
        sources.append(approval)
        self.consume(approval, claim=True)
        brief = build_project_brief(self.store, self.project,
            query='What should the next person know following the rehearsal?', budget=6000)
        self.assertFalse(brief['source_evidence_requested'])
        self.assertTrue(brief['user_source_coverage_requested'])
        for source in (actual, report):
            self.assertTrue(self.visible(brief['xml'], source), (source['body'], brief['xml']))
        self.assertTrue(self.visible(brief['xml'], sources[0]), brief['xml'])
        self.assertTrue(self.visible(brief['xml'], sources[1]), brief['xml'])
        self.assertEqual(brief['xml'], render_project_brief(brief, budget=6000))
        self.assertLessEqual(len(brief['xml']), 6000)

    def test_unknown_policy_paraphrase_keeps_originals_when_primary_roles_and_rules_fit(self):
        policy = self.user('The telescope export must preserve instrument units. Add an optional paper chart.', 'policy')
        self.consume(policy)
        revised = self.user('Continue the telescope export. Remove the paper chart while preserving instrument units.', 'revision')
        self.consume(revised)
        memory = ProjectMemory(self.store)
        self.tool('Telescope fixture: instrument units retained. Device delivery was not attempted.', 91)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        self.store.remember(self.project, 'Assistant report', 'The telescope export is complete.',
                            source='hook:Stop', session_id='report')
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        brief = build_project_brief(self.store, self.project,
            query='Where does that leave the telescope arrangement?', budget=6000)
        self.assertFalse(brief['source_evidence_requested'])
        self.assertTrue(self.visible(brief['xml'], policy), brief['xml'])
        self.assertTrue(self.visible(brief['xml'], revised), brief['xml'])
        self.assertEqual(brief['xml'], render_project_brief(brief, budget=6000))
        self.assertLessEqual(len(brief['xml']), 6000)

    def test_unknown_primary_execution_protection_scales_with_capacity_without_fragmenting_capture(self):
        memory = ProjectMemory(self.store)
        policy = self.user('The observatory export must retain original event labels. The reviewers compare labels with paper logs, so translated labels would break their references.', 'policy')
        self.consume(policy)
        revision = self.user('Continue the observatory export. Remove the optional diagram, but retain the original event labels and the independent reviewer signoff.', 'revision')
        self.consume(revision)
        observed = self.tool('Observatory fixture: original labels retained. Remote delivery was not attempted. Reviewer signoff was not established by this check.', 101)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        query = 'Where does that leave the observatory arrangement?'
        for budget in (2600, 2900, 5900):
            with self.subTest(budget=budget):
                brief = build_project_brief(self.store, self.project, query=query, budget=budget)
                self.assertFalse(brief['source_evidence_requested'])
                xml = render_project_brief(brief, budget=budget)
                self.assertTrue(self.visible(xml, policy), xml)
                self.assertTrue(self.visible(xml, revision), xml)
                nodes = [item for item in ET.fromstring(xml).findall('item') if item.find('execution') is not None]
                if budget == 5900:
                    self.assertTrue(nodes, xml)
                for item in nodes:
                    refs = {ref.get('source_field'): ref for ref in item.iter('source')}
                    self.assertEqual(refs['tool_response.output'].findtext('quote_excerpt'), observed['body'])
                    self.assertEqual(refs['tool_input.cmd'].findtext('quote_excerpt'), 'python check.py ORB-27 --run 101')
                    self.assertEqual(item.find('execution').get('exit_code'), '0')
                    self.assertNotIn('fields_omitted', item.attrib)
                if not nodes:
                    self.assertIsNotNone(ET.fromstring(xml).find('omitted'), xml)
                self.assertEqual(brief['xml'], xml)
                self.assertLessEqual(len(xml), budget)

    def test_three_neutral_originals_keep_middle_revision_without_asserting_identity(self):
        memory = ProjectMemory(self.store)
        rows = []
        for index, text in enumerate(('Add a visual timetable for the workshop.',
                                      'Withdraw the optional timetable. Retain the printed instructions.',
                                      'An independent coordinator must approve the sample before delivery.')):
            row = self.user(text, f'neutral-{index}')
            rows.append(row)
            memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        brief = build_project_brief(self.store, self.project,
            query='Where does the observatory arrangement stand?', budget=6000)
        xml = render_project_brief(brief, budget=6000)
        self.assertEqual(len(brief['unassociated_user_context']), 3)
        for row in rows:
            self.assertTrue(self.visible(xml, row), xml)
        tree = ET.fromstring(xml)
        for item in tree.findall('item'):
            if item.get('selection_basis') != 'unassociated_user_context':
                continue
            self.assertEqual(item.get('relationship_to_query'), 'unresolved')
            self.assertEqual(item.get('is_requirement_claim'), 'False')
            self.assertIsNone(item.get('workflow_state'))
            for source in item.iter('source'):
                row = next(row for row in rows if row['id'] == source.get('source_id'))
                self.assertEqual((source.get('start'), source.get('end')), ('0', str(len(row['body']))))
                self.assertEqual(source.get('source_role'), 'user_intent')
        self.assertEqual(brief['unassociated_user_context_coverage']['selected_source_count'], 3)
        self.assertEqual(brief['unassociated_user_context_coverage']['omitted'], 0)
        self.assertEqual(brief['relations'], [])
        self.assertEqual(brief['xml'], xml)
        self.assertLessEqual(len(xml), 6000)

    def test_neutral_third_slot_retains_size_budget_and_fourth_omission_with_distinct_scopes(self):
        memory = ProjectMemory(self.store)
        rows = []
        for index in range(4):
            row = self.user(f'Keep independent sample {index} available for review.', f'neutral-{index}')
            rows.append(row)
            memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        brief = build_project_brief(self.store, self.project,
            query='Where does the observatory arrangement stand?', budget=6000)
        # Renderer qualifiers remain distinct even when their exact originals
        # compete in the same already-selected neutral pool.
        brief['unassociated_user_context'][0]['scope'] = {'environment': 'local', 'version': '1.2.0'}
        xml = render_project_brief(brief, budget=6000)
        tree = ET.fromstring(xml)
        marker = tree.find('source-context-coverage')
        self.assertEqual(marker.get('selected_source_count'), '3')
        self.assertEqual(marker.get('omitted'), '1')
        self.assertTrue(self.visible(xml, rows[0]), xml)
        self.assertTrue(self.visible(xml, rows[-1]), xml)
        self.assertFalse(self.visible(xml, rows[2]), xml)
        neutral = [item for item in tree.findall('item')
                   if item.get('selection_basis') == 'unassociated_user_context']
        known = [item for item in neutral if item.get('environment') == 'local']
        unknown = [item for item in neutral if item.get('environment') is None]
        self.assertEqual(len(known), 1)
        self.assertTrue(unknown)
        self.assertEqual(known[0].get('version'), '1.2.0')
        self.assertTrue(all(item.get('relationship_to_query') == 'unresolved' for item in neutral))
        for budget in (600, 2900, 6000):
            rendered = render_project_brief(brief, budget=budget)
            parsed = ET.fromstring(rendered)
            selected = [item for item in parsed.findall('item')
                        if item.get('selection_basis') == 'unassociated_user_context']
            spent = sum(len(ET.tostring(item, encoding='unicode', short_empty_elements=True).replace(' />', '/>'))
                        for item in selected)
            self.assertLessEqual(spent, min(1800, budget // 3))
            self.assertLessEqual(len(rendered), budget)
            if budget == 600:
                self.assertFalse(selected)
                self.assertIsNotNone(parsed.find('omitted'))

    def test_neutral_three_slots_never_override_1800_allowance_or_split_original(self):
        memory = ProjectMemory(self.store)
        rows = []
        for index in range(3):
            text = f'Independent sample {index}. ' + 'Keep the original measurement for review. ' * 10
            row = self.user(text, f'large-neutral-{index}')
            rows.append(row)
            memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        brief = build_project_brief(self.store, self.project,
            query='Where does the observatory arrangement stand?', budget=6000)
        tree = ET.fromstring(brief['xml'])
        shown = [row for row in rows if self.visible(brief['xml'], row)]
        self.assertGreater(len(shown), 0)
        self.assertLess(len(shown), 3)
        for source in tree.iter('source'):
            if source.get('source_id') in {row['id'] for row in rows}:
                row = next(row for row in rows if row['id'] == source.get('source_id'))
                self.assertEqual(source.findtext('quote_excerpt'), row['body'])
                self.assertEqual(source.get('end'), str(len(row['body'])))
        self.assertEqual(brief['unassociated_user_context_coverage']['omitted'], 3 - len(shown))
        self.assertEqual(brief['xml'], render_project_brief(brief, budget=6000))

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
