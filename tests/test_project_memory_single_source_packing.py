"""Public toy contracts for single-charge original transport at the render seam."""
from copy import deepcopy
import unittest
from xml.etree import ElementTree as ET

from codex_mem.project_memory_retrieval import render_project_brief
import test_project_memory_compact_execution_transport as native_support


def original(source_id='public-source', text='Keep raw measurements.', role='user_intent', field='body'):
    return {'source_id': source_id, 'source_role': role, 'source_field': field,
            'start': 0, 'end': len(text), 'quote_excerpt': text, 'quote_truncated': False}


def episode(record_id, pointer, **qualifiers):
    return {'id': record_id, 'lane': 'episode', **qualifiers, 'fields': [
        {'name': 'goal', 'basis': 'attributed_source_excerpts',
         'field_association': 'model_selected', 'evidence': [deepcopy(pointer)]}]}


class SingleSourcePackingTests(unittest.TestCase):
    def render(self, records, budget=6000, **extra):
        xml = render_project_brief({'records': records, 'user_source_coverage_requested': True,
                                    **extra}, budget=budget)
        self.assertLessEqual(len(xml), budget)
        return ET.fromstring(xml), xml

    def test_quote_reference_meaning_is_explicit_and_association_context_stays_local(self):
        pointer = original(text='Review <raw> & baseline samples.')
        tree, xml = self.render([episode('canonical-a', pointer, uncertainty='pending'),
                                 episode('canonical-b', pointer, uncertainty='historical')])
        self.assertEqual('provenance_only', tree.get('quote_refs'), xml)
        self.assertEqual('historical', tree.find("./item[@id='canonical-b']").get('uncertainty'))

    def test_mandatory_reference_note_has_a_bounded_primary_budget_fallback(self):
        from unittest.mock import patch
        from codex_mem import project_memory_retrieval as renderer
        pointer = original(text='Preserve <raw> & baseline records.')
        brief = {'records': [episode('canonical-a', pointer), episode('canonical-b', pointer),
                             episode('canonical-c', pointer)], 'user_source_coverage_requested': True}
        canonical = renderer._render_project_brief(brief)[0]
        for budget in (len(canonical), len(canonical) - 1):
            with patch.object(renderer, '_render_project_brief', wraps=renderer._render_project_brief) as primary:
                xml, actual_omissions = renderer._compact_and_refill_sources(brief, canonical, budget)
            tree = ET.fromstring(xml)
            self.assertLessEqual(len(xml), budget)
            self.assertEqual(1, primary.call_count)
            self.assertEqual(budget - len(' quote_refs="provenance_only"'), primary.call_args.args[1])
            self.assertEqual('provenance_only', tree.get('quote_refs'), xml)
            self.assertIsNotNone(tree.find('omitted'), xml)
            for key, value in actual_omissions.items():
                self.assertEqual(value, int(tree.find('omitted').get(key, '0')))
            registry = {node.get('id'): node for node in tree.iter('source') if node.get('id')}
            for reference in tree.iter('quote_ref'):
                self.assertIn(reference.get('ref'), registry, xml)
                self.assertEqual(pointer['quote_excerpt'], registry[reference.get('ref')].findtext('quote_excerpt'))

    def test_only_unreferenced_synthetic_display_identity_is_removed(self):
        from codex_mem.project_memory_source_transport import compact_selected_xml
        node = '<project-memory><item id="selected-source-1" lane="source_excerpts"><field><source source_id="own" source_role="user_intent" source_field="body" start="0" end="3"><quote_excerpt>raw</quote_excerpt></source></field></item></project-memory>'
        self.assertEqual(node, compact_selected_xml(node, {'selected-source-1'}, native_tools=frozenset()))
        compact = ET.fromstring(compact_selected_xml(node, set(), native_tools=frozenset()))
        self.assertIsNone(compact.find('item').get('id'))
        self.assertEqual('own', compact.find('.//source').get('source_id'))
        canonical = node.replace('selected-source-1', 'canonical-episode')
        self.assertEqual(canonical, compact_selected_xml(canonical, set(), native_tools=frozenset()))

    def test_shared_original_keeps_both_independently_qualified_associations(self):
        pointer = original(text='Retain <raw> & calibrated measurements.')
        records = [episode('canonical-a', pointer, uncertainty='review_pending'),
                   episode('canonical-b', pointer, uncertainty='review_pending')]
        tree, xml = self.render(records)
        quotes = [node for node in tree.iter('source')
                  if node.findtext('quote_excerpt') == pointer['quote_excerpt']]
        self.assertEqual(1, len(quotes), xml)
        for record_id in ('canonical-a', 'canonical-b'):
            item = tree.find(f"./item[@id='{record_id}']")
            self.assertIsNotNone(item, xml)
            field = item.find("./field[@name='goal']")
            self.assertIsNotNone(field, xml)
            self.assertEqual('model_selected', field.get('field_association'), xml)
            self.assertTrue(list(field), xml)
        for key in ('source_id', 'source_role', 'source_field', 'start', 'end'):
            self.assertEqual(str(pointer[key]), quotes[0].get(key), xml)
        registry = {source.get('id'): source for source in tree.iter('source')
                    if source.get('id')}
        for reference in tree.iter('quote_ref'):
            self.assertIn(reference.get('ref'), registry, xml)
            self.assertEqual('quote_ref', reference.tag)

    def test_same_original_different_scope_and_session_never_loses_association_context(self):
        pointer = original()
        first = episode('canonical-a', pointer, scope={'environment': 'lab'},
                        uncertainty='review_pending', session_id='session-a')
        second = episode('canonical-b', pointer, scope={'environment': 'field'},
                         uncertainty='review_pending', session_id='session-b')
        for records in ([first, second], [second, first]):
            with self.subTest(order=[record['id'] for record in records]):
                tree, xml = self.render(records)
                for record in records:
                    item = tree.find(f"./item[@id='{record['id']}']")
                    self.assertIsNotNone(item, xml)
                    self.assertEqual(record['scope']['environment'], item.get('environment'), xml)
                    self.assertIsNotNone(item.find("./field[@name='goal']"), xml)

    def test_custody_history_and_unknown_context_stay_on_their_own_associations(self):
        pointer = original()
        declarations = [
            {'id': 'stored', 'lane': 'intent_custody', 'source_role': 'user_intent',
             'disposition': 'stored', 'meaning': 'original_user_observation',
             'is_requirement_claim': False, 'uncertainty': 'current_applicability_unresolved'},
            {'id': 'completed', 'lane': 'intent_custody', 'source_role': 'user_intent',
             'disposition': 'completed', 'meaning': 'original_user_observation',
             'is_requirement_claim': False, 'uncertainty': 'historical_applicability_unknown'},
            {'id': 'history', 'lane': 'episode', 'is_current_head': False,
             'episode_version': 1, 'uncertainty': 'historical_applicability_unknown'}]
        records = [{**declaration, 'fields': [
            {'name': 'prior_goal' if declaration['id'] == 'history' else 'source_custody',
             'basis': 'attributed_source_excerpts', 'field_association': 'not_assessed',
             'evidence': [deepcopy(pointer)]}]} for declaration in declarations]
        for ordered in (records, list(reversed(records))):
            tree, xml = self.render(ordered)
            for declaration in declarations:
                item = tree.find(f"./item[@id='{declaration['id']}']")
                # A source-only custody container can replace a synthetic custody
                # record, but its own custody qualifiers must remain visible.
                if item is None and declaration['lane'] == 'intent_custody':
                    item = tree.find(f"./item[@disposition='{declaration['disposition']}']")
                self.assertIsNotNone(item, xml)
                for key in ('disposition', 'meaning', 'is_requirement_claim', 'uncertainty',
                            'is_current_head', 'episode_version'):
                    if key in declaration:
                        self.assertEqual(str(declaration[key]), item.get(key), xml)
            self.assertEqual('not_verified', tree.get('freshness'))

    def test_every_reference_resolves_after_budget_omissions(self):
        records = [episode('canonical-a', original(text='A <sample> & café. ' * 12)),
                   episode('canonical-b', original(text='A <sample> & café. ' * 12))]
        for budget in (128, 512, 1000, 2900, 6000):
            tree, xml = self.render(records, budget=budget)
            registry = {source.get('id'): source for source in tree.iter('source')
                        if source.get('id') and source.find('quote_excerpt') is not None}
            for reference in tree.iter('quote_ref'):
                self.assertIn(reference.get('ref'), registry, xml)
                source = registry[reference.get('ref')]
                for key in ('source_id', 'source_role', 'source_field', 'start', 'end'):
                    self.assertIsNotNone(source.get(key), xml)
            if tree.find("./item[@id='canonical-a']/field") is None:
                self.assertTrue(tree.find('omitted') is not None or budget == 128, xml)

    def test_quote_reference_shares_only_provenance_with_a_derived_association(self):
        pointer = original()
        historical = episode('s1', pointer, scope={'environment': 'lab'},
                             is_current_head=False, episode_version=1,
                             uncertainty='historical_applicability_unknown')
        claim = {'id': 'canonical-claim', 'lane': 'claim', 'inferred': True,
                 'scope': {'environment': 'production'}, 'revision': 4,
                 'uncertainty': 'association_unverified', 'fields': [
                     {'name': 'text', 'text': 'The team requested retention of raw measurements.',
                      'basis': 'model_inference', 'field_association': 'model_selected',
                      'evidence': [deepcopy(pointer)]}]}
        tree, xml = self.render([historical, claim])
        item = tree.find("./item[@id='canonical-claim']")
        self.assertEqual('production', item.get('environment'), xml)
        self.assertEqual('True', item.get('inferred'), xml)
        self.assertEqual('association_unverified', item.get('uncertainty'), xml)
        self.assertIsNone(item.get('is_current_head'), xml)
        field = item.find("./field[@name='text']")
        self.assertEqual('model_inference', field.get('basis'), xml)
        self.assertEqual('model_selected', field.get('field_association'), xml)
        self.assertEqual(claim['fields'][0]['text'], field.findtext('derived_text'), xml)
        reference = field.find('quote_ref')
        self.assertIsNotNone(reference, xml)
        targets = [source for source in tree.iter('source') if source.get('id') == reference.get('ref')]
        self.assertEqual(1, len(targets), xml)
        self.assertEqual(pointer['quote_excerpt'], targets[0].findtext('quote_excerpt'), xml)
        self.assertIsNotNone(tree.find("./item[@id='s1']"), xml)
        self.assertIsNone(item.get('disposition'), xml)
        self.assertIsNone(item.get('workflow_state'), xml)

    def test_quote_reference_preserves_its_own_context_basis_and_unknown_qualifiers(self):
        pointer = original('public-context')
        contextual = {**pointer, 'field': 'goal',
                      'context_basis': 'same_source_adjacent_user_statement',
                      'association_note': 'Applicability remains unresolved & unverified.',
                      'optional_association_context': ''}
        tree, xml = self.render([episode('canonical-original', pointer),
                                 episode('canonical-adjacent', contextual)])
        field = tree.find("./item[@id='canonical-adjacent']/field[@name='goal']")
        reference = field.find('quote_ref')
        self.assertIsNotNone(reference, xml)
        for key in ('context_basis', 'association_note', 'optional_association_context'):
            self.assertEqual(contextual[key], reference.get(key), xml)
        ordinary_reference = tree.find("./item[@id='canonical-original']/field/quote_ref")
        self.assertIsNotNone(ordinary_reference, xml)
        self.assertIsNone(ordinary_reference.get('context_basis'), xml)
        self.assertIsNone(ordinary_reference.get('association_note'), xml)
        shown = [source for source in tree.iter('source') if source.get('source_id') == pointer['source_id']
                 and source.find('quote_excerpt') is not None]
        self.assertEqual(1, len(shown), xml)

    def test_a_partial_quote_keeps_its_explicit_truncation_warning(self):
        pointer = {**original(), 'quote_truncated': True}
        tree, xml = self.render([episode('partial', pointer)])
        sources = list(tree.iter('source'))
        self.assertTrue(sources, xml)
        self.assertTrue(all(source.get('quote_truncated') == 'True' for source in sources), xml)
        self.assertEqual([], list(tree.iter('quote_ref')), xml)

    def test_full_custody_original_precedes_a_selected_partial_of_its_own_body(self):
        text = 'Retain the calibration record. Approval is still pending. Delivery is not established.'
        full = original('public-full', text)
        selected = {**full, 'start': text.index('Approval'), 'end': text.index(' Delivery'),
                    'quote_excerpt': 'Approval is still pending.'}
        material = episode('canonical-partial', selected, uncertainty='association_unverified')
        custody = {'id': 'public-full', 'lane': 'intent_custody', 'source_role': 'user_intent',
                   'disposition': 'stored', 'is_requirement_claim': False,
                   'meaning': 'original_user_observation', 'uncertainty': 'current_applicability_unresolved',
                   'fields': [{'name': 'source_custody', 'basis': 'attributed_source_excerpts',
                               'evidence': [full]}]}
        tree, xml = self.render([material, custody], budget=2900)
        shown = [source for source in tree.iter('source') if source.get('source_id') == full['source_id']
                 and source.find('quote_excerpt') is not None]
        self.assertEqual([text], [source.findtext('quote_excerpt') for source in shown], xml)
        association = tree.find("./item[@id='canonical-partial']")
        self.assertEqual('association_unverified', association.get('uncertainty'), xml)
        reference = association.find("./field[@name='goal']/quote_ref")
        self.assertIsNotNone(reference, xml)
        self.assertEqual((str(selected['start']), str(selected['end'])),
                         (reference.get('start'), reference.get('end')), xml)

    def test_whole_source_budget_fallback_keeps_nonzero_risk_and_omission_warnings(self):
        coverage = {'complete_transcript': False, 'observations_only': True,
                    'coverage_gaps': True, 'pending_reconciliation': True,
                    'quarantined_events': 2, 'omitted_transitions_count': 1,
                    'source_evidence_omitted': 3, 'user_sources_omitted': 2,
                    'downgraded_states_count': 0, 'partially_processed_events': 0,
                    'processed_through_seq': 99}
        pointer = original(text='Retain the original samples before independent review. ' * 7)
        tree, xml = self.render([episode('canonical-risk', pointer)], budget=700,
                                coverage=coverage, pending=True)
        shown = tree.find('coverage')
        for key, expected in {'complete_transcript': 'false', 'observations_only': 'true',
                              'coverage_gaps': 'true', 'pending_reconciliation': 'true',
                              'pending': 'true', 'quarantined_events': '2',
                              'omitted_transitions_count': '1', 'source_evidence_omitted': '3',
                              'user_sources_omitted': '2'}.items():
            self.assertEqual(expected, shown.get(key), xml)
        if not any(source.findtext('quote_excerpt') == pointer['quote_excerpt']
                   for source in tree.iter('source')):
            self.assertIsNotNone(tree.find('omitted'), xml)

    def test_same_text_different_events_roles_and_fields_remain_citable(self):
        pointers = [original('public-a'), original('public-b'),
                    original('public-a', role='assistant_report'),
                    original('public-a', field='tool_response.output')]
        tree, xml = self.render([episode(f'canonical-{i}', ref) for i, ref in enumerate(pointers)])
        observed = {(ref.get('source_id'), ref.get('source_role'), ref.get('source_field'))
                    for ref in tree.iter('source') if ref.find('quote_excerpt') is not None}
        self.assertEqual({(ref['source_id'], ref['source_role'], ref['source_field'])
                          for ref in pointers}, observed, xml)

    def test_escaped_original_and_atomic_execution_fit_or_omit_whole(self):
        command = original('public-tool', 'python check.py --name "A&B"', 'tool_record', 'tool_input.cmd')
        output = original('public-tool', 'Observed <sample> & café.\n' * 7, 'tool_record', 'tool_response.output')
        for ref in (command, output):
            ref['tool_use_id'] = 'public-call'
        record = {'id': 'public-tool', 'lane': 'source_evidence', 'source_role': 'tool_record',
                  'execution': {'tool_name': 'functions.exec_command', 'tool_use_id': 'public-call',
                                'command': command['quote_excerpt'], 'exit_code': 0},
                  'fields': [{'name': ref['source_field'], 'basis': 'attributed_source_excerpts',
                              'field_association': 'original_execution_capture', 'evidence': [ref]}
                             for ref in (command, output)]}
        full, full_xml = self.render([record])
        self.assertIsNotNone(full.find('.//execution'), full_xml)
        for budget in (len(full_xml), len(full_xml) - 1, 2900, 6000):
            tree, xml = self.render([record], budget=budget)
            execution = tree.find('.//execution')
            sources = list(tree.iter('source'))
            if execution is None:
                self.assertEqual([], sources, xml)
                self.assertIsNotNone(tree.find('omitted'), xml)
            else:
                self.assertEqual('public-call', execution.get('tool_use_id'))
                self.assertEqual('0', execution.get('exit_code'))
                self.assertEqual({command['quote_excerpt'], output['quote_excerpt']},
                                 {ref.findtext('quote_excerpt') for ref in sources}, xml)


class NativeOriginalPackingTests(unittest.TestCase):
    setUp = native_support.CompactExecutionTransportTests.setUp
    tool = native_support.CompactExecutionTransportTests.tool
    prepare = native_support.CompactExecutionTransportTests.prepare

    def test_historical_reference_does_not_disqualify_a_complete_custody_original(self):
        _, brief = self.prepare()
        first = brief['records'][0]
        additional = deepcopy(first)
        additional['id'] = 'public-additional'
        additional['execution']['tool_use_id'] = 'public-additional-call'
        for field in additional['fields']:
            for pointer in field['evidence']:
                pointer.update(source_id=additional['id'], tool_use_id='public-additional-call')
        old = original('public-history', 'Keep the original chart for the archive only. It does not establish current approval or delivery.')
        current = original('public-current', 'The current sample still requires independent review.')
        custody = {'id': 'public-history', 'lane': 'intent_custody', 'source_role': 'user_intent',
                   'disposition': 'completed', 'meaning': 'original_user_observation',
                   'is_requirement_claim': False, 'uncertainty': 'current_applicability_unresolved',
                   'fields': [{'name': 'source_custody', 'basis': 'attributed_source_excerpts',
                               'evidence': [old]}]}
        history = {'id': 'canonical-history', 'lane': 'episode', 'is_current_head': False,
                   'episode_version': 1, 'uncertainty': 'historical_applicability_unknown',
                   'fields': [{'name': 'prior_goal', 'basis': 'attributed_source_excerpts',
                               'field_association': 'model_inference', 'evidence': [old]}]}
        claim = {'id': 'canonical-current', 'lane': 'claim', 'workflow_state': 'open',
                 'source_role': 'user_intent', 'fields': [
                     {'name': 'text', 'basis': 'attributed_source_excerpts',
                      'field_association': 'model_selected', 'evidence': [current]}]}
        xml = render_project_brief({'records': [first, additional, custody, history, claim],
                                   'user_source_coverage_requested': True,
                                   'source_evidence_requested': True}, budget=2900)
        tree = ET.fromstring(xml)
        containers = [item for item in tree.findall('item') if any(
            source.get('source_id') == old['source_id'] and source.findtext('quote_excerpt') == old['quote_excerpt']
            for source in item.iter('source'))]
        self.assertTrue(containers, xml)
        self.assertEqual(1, sum(1 for source in tree.iter('source') if
                              source.get('source_id') == old['source_id'] and source.find('quote_excerpt') is not None), xml)
        for container in containers:
            self.assertEqual('completed', container.get('disposition'), xml)
            self.assertEqual('False', container.get('is_requirement_claim'), xml)
            self.assertEqual('current_applicability_unresolved', container.get('uncertainty'), xml)
            self.assertIsNone(container.get('workflow_state'), xml)
            self.assertIsNone(container.get('is_current_head'), xml)
        historical = tree.find("./item[@id='canonical-history']")
        self.assertIsNotNone(historical, xml)
        self.assertEqual('False', historical.get('is_current_head'), xml)
        self.assertEqual('historical_applicability_unknown', historical.get('uncertainty'), xml)
        self.assertTrue(historical.find("./field[@name='prior_goal']") is not None or
                        int(historical.get('fields_omitted', '0')) > 0, xml)
        active = tree.find("./item[@id='canonical-current']")
        self.assertIsNotNone(active, xml)
        self.assertEqual('open', active.get('workflow_state'), xml)
        self.assertIsNone(active.get('disposition'), xml)
        registry = {source.get('id'): source for source in tree.iter('source') if source.get('id')}
        for reference in active.iter('quote_ref'):
            self.assertEqual(current['source_id'], registry[reference.get('ref')].get('source_id'), xml)
        self.assertLessEqual(len(xml), 2900)

    def test_explicit_capture_order_controls_the_first_protected_execution(self):
        _, brief = self.prepare()
        early = brief['records'][0]
        later = deepcopy(early)
        later.update(id='public-later', source_capture_seq_min=9, source_capture_seq_max=9)
        later['execution']['tool_use_id'] = 'public-later-call'
        for field in later['fields']:
            for pointer in field['evidence']:
                pointer.update(source_id=later['id'], tool_use_id='public-later-call')
        for order, expected in (('earliest', early['id']), ('latest', later['id'])):
            xml = render_project_brief({'records': [early, later], 'source_order': order,
                                       'source_evidence_requested': True}, budget=2900)
            tree = ET.fromstring(xml)
            self.assertEqual(expected, tree.find('./item[execution]').get('id'), xml)
            self.assertTrue(all(item.get('workflow_state') is None
                                for item in tree.findall('./item[execution]')), xml)

    def test_additional_native_groups_cannot_displace_a_fitting_whole_user_constraint(self):
        _, brief = self.prepare()
        first = brief['records'][0]
        additional = []
        for index in range(2):
            record = deepcopy(first)
            record['id'] = f'public-additional-{index}'
            record['execution']['tool_use_id'] = f'public-call-{index}'
            for field in record['fields']:
                for pointer in field['evidence']:
                    pointer.update(source_id=record['id'], tool_use_id=record['execution']['tool_use_id'])
            additional.append(record)
        pointer = original('public-user', 'Retain the raw calibration record and obtain independent approval before delivery. ' * 6)
        custody = {'id': 'public-user', 'lane': 'intent_custody', 'source_role': 'user_intent',
                   'disposition': 'unresolved', 'meaning': 'original_user_source_with_unreconciled_representation',
                   'is_requirement_claim': False, 'uncertainty': 'extracted_representation_completeness_unresolved',
                   'source_capture_seq_min': 4, 'source_capture_seq_max': 4, 'fields': [
                       {'name': 'source_custody', 'basis': 'attributed_source_excerpts',
                        'evidence': [pointer]}]}
        for natives in ([first], [first, *additional]):
            xml = render_project_brief({'records': [*natives, custody],
                                       'source_evidence_requested': True,
                                       'user_source_coverage_requested': True}, budget=2900)
            tree = ET.fromstring(xml)
            self.assertTrue(any(source.get('source_id') == pointer['source_id'] and
                                source.findtext('quote_excerpt') == pointer['quote_excerpt']
                                for source in tree.iter('source')), xml)
            self.assertIsNotNone(tree.find(f"./item[@id='{first['id']}']/execution"), xml)
            self.assertLessEqual(len(xml), 2900)

    def test_postselection_native_compaction_preserves_atomic_identity_and_unknown_metadata(self):
        from codex_mem.project_memory_retrieval import _execution_xml, _EXECUTION_TOOLS
        from codex_mem.project_memory_source_transport import compact_selected_xml
        _, brief = self.prepare()
        record = brief['records'][0]
        for role, signal in [('tool_record', 'observed_execution_result'),
                             ('unknown', 'unverified'), ('assistant_report', 'observed_execution_result')]:
            sample = deepcopy(record)
            sample.update(source_role=role, signal_kind=signal)
            node = _execution_xml(sample, None)
            compact = compact_selected_xml('<project-memory>'+node+'</project-memory>', set(), native_tools=_EXECUTION_TOOLS)
            tree = ET.fromstring(compact)
            item = tree.find('item')
            self.assertLess(len(compact), len('<project-memory>'+node+'</project-memory>'))
            self.assertEqual(None if role == 'tool_record' else role, item.get('source_role'))
            self.assertEqual(None if signal == 'observed_execution_result' else signal, item.get('signal_kind'))
            self.assertEqual('true', item.get('complete_capture_fields'))
            before = ET.fromstring(node)
            self.assertEqual(ET.tostring(before.find('execution')), ET.tostring(item.find('execution')))
            for old, new in zip(before.findall('source'), item.findall('source')):
                for key in ('source_id', 'source_role', 'source_field', 'tool_use_id', 'start', 'end'):
                    self.assertEqual(old.get(key), new.get(key))
                self.assertEqual(old.findtext('quote_excerpt'), new.findtext('quote_excerpt'))
        for key, value in [('start', '1'), ('quote_truncated', 'True'),
                           ('source_role', 'unknown'), ('tool_use_id', 'conflicting-call'),
                           ('source_field', 'body')]:
            malformed = ET.fromstring(_execution_xml(record, None))
            malformed.find('source').set(key, value)
            xml = '<project-memory>'+ET.tostring(malformed, encoding='unicode')+'</project-memory>'
            self.assertEqual(xml, compact_selected_xml(xml, set(), native_tools=_EXECUTION_TOOLS))
        malformed = ET.fromstring(_execution_xml(record, None))
        malformed.remove(malformed.findall('source')[1])
        xml = '<project-memory>'+ET.tostring(malformed, encoding='unicode')+'</project-memory>'
        self.assertEqual(xml, compact_selected_xml(xml, set(), native_tools=_EXECUTION_TOOLS))

    def test_refill_retains_the_primary_catalog_and_associations_at_exact_wire_cost(self):
        from codex_mem import project_memory_retrieval as renderer
        _, brief = self.prepare()
        text = 'Calibration <raw> & stable. Approval remains pending. No delivery was attempted.'
        pointer = original('public-other-tool', text, 'tool_record')
        brief['records'] += [episode('canonical-summary', {**pointer, 'end': 25, 'quote_excerpt': text[:25]}),
            episode('canonical-result', {**pointer, 'start': 25, 'quote_excerpt': text[25:]})]
        retained = False
        canonical = renderer._render_project_brief(brief, 1400)[0]
        for budget in range(1400, 1901):
            xml, actual_omissions = renderer._compact_and_refill_sources(brief, canonical, budget)
            before, after = ET.fromstring(canonical), ET.fromstring(xml)
            old_sources = [ET.tostring(node) for node in before.iter('source')]
            new_sources = list(after.iter('source'))
            for old in before.iter('source'):
                self.assertTrue(any(all(old.get(key) == new.get(key) for key in
                    ('source_id', 'source_role', 'source_field', 'tool_use_id', 'start', 'end')) and
                    old.findtext('quote_excerpt') == new.findtext('quote_excerpt') for new in new_sources), xml)
            for item in before.findall('item'):
                if item.get('lane') == 'episode':
                    self.assertEqual(ET.tostring(item), ET.tostring(after.find("./item[@id='%s']" % item.get('id'))))
            self.assertEqual(ET.tostring(before.find('omitted')), ET.tostring(after.find('omitted')))
            self.assertLessEqual(len(xml), budget)
            fresh = [node for node in new_sources if node.get('source_id') == pointer['source_id'] and
                     node.findtext('quote_excerpt') == text and ET.tostring(node) not in old_sources]
            if fresh:
                retained = True
                exact = renderer._compact_and_refill_sources(brief, canonical, len(xml))[0]
                below = renderer._compact_and_refill_sources(brief, canonical, len(xml) - 1)[0]
                self.assertEqual(xml, exact)
                self.assertFalse(any(node.get('source_id') == pointer['source_id'] and
                    node.findtext('quote_excerpt') == text for node in ET.fromstring(below).iter('source')))
                break
        self.assertTrue(retained, 'At least one boundary must refill a whole escaped body without evicting the primary set.')

    def test_native_output_is_not_replaced_by_a_body_field_mirror(self):
        source, brief = self.prepare()
        material = {'id': 'canonical-material', 'lane': 'episode', 'fields': [
            {'name': 'outcome', 'basis': 'attributed_source_excerpts',
             'field_association': 'model_inference',
             'evidence': [original(source['id'], source['body'], 'tool_record')]}]}
        brief.update(records=[material, *brief['records']], source_evidence_requested=True)
        xml = render_project_brief(brief)
        tree = ET.fromstring(xml)
        pointers = [pointer for pointer in tree.iter('source')
                    if pointer.get('source_id') == source['id'] and
                    pointer.findtext('quote_excerpt') == source['body']]
        self.assertEqual({'body', 'tool_response.output'},
                         {pointer.get('source_field') for pointer in pointers}, xml)
        execution = tree.find('.//execution')
        self.assertIsNotNone(execution, xml)
        self.assertEqual('1', execution.get('exit_code'), xml)
        self.assertEqual('functions.exec_command', execution.get('tool_name'), xml)
        command = tree.find(".//source[@source_field='tool_input.cmd']")
        self.assertEqual('python check.py ORB-27 --run 71', command.findtext('quote_excerpt'), xml)
        self.assertTrue(all(pointer.get('source_role') == 'tool_record' for pointer in pointers))


if __name__ == '__main__':
    unittest.main()
