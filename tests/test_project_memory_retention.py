"""Whole-field retention through processing and project briefs, without models."""
import json
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree

from codex_mem.config import configure
from codex_mem.episode_processor import process_project_memory
from codex_mem.project_memory import ProjectMemory
from codex_mem.project_memory_retrieval import build_project_brief
from codex_mem.store import Store
from test_episode_processor import receipt
from source_transport_assertions import SourceRegistry


class ProjectMemoryRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / 'project'
        self.project.mkdir()
        self.data = Path(temporary.name) / 'memory'
        configure(self.data, capture_scope='all', project_memory_mode='pilot',
                  project_memory_projects=[str(self.project)], jev_quality_enabled=False)

    def process_fields(self, fields, *, project=None, session='first', incidental=0, continuation=None):
        workspace = project or self.project
        values = list(dict.fromkeys(fields.values()))
        body = '\n'.join(value for value in values
                         if not any(value != other and value in other for other in values))
        with Store(self.data) as store:
            source = store.remember(workspace, 'Task RET-17', body,
                                    source='hook:UserPromptSubmit', session_id=session)

        def generate(request):
            supplied = json.loads(request['prompt'].split('\n', 1)[1])
            event = next(event for event in supplied['events'] if event['id'] == source['id'])
            spans = event['spans']
            episode = {name: fields.get(name, '') for name in
                       ('summary', 'goal', 'rationale', 'outcome', 'open_work')}
            episode['continuation_of'] = continuation
            episode['evidence'] = {name: [span['ref'] for span in spans
                if span['text'].strip() and span['text'].strip() in value]
                for name, value in episode.items() if name != 'continuation_of'}
            episode['evidence']['continuation_of'] = [spans[0]['ref']] if continuation else []
            extras = [{'summary': fields['summary'], 'goal': '', 'rationale': '', 'outcome': '',
                       'open_work': '', 'continuation_of': None,
                       'evidence': {name: episode['evidence'][name] if name == 'summary' else []
                                    for name in episode['evidence']}} for _ in range(incidental)]
            return receipt({'episodes': [episode] + extras, 'claims': [], 'relations': []})

        result = process_project_memory(workspace, self.data, runner=generate)
        self.assertEqual(result['status'], 'processed', result)
        with Store(self.data) as store:
            episode = next(item for item in ProjectMemory(store).snapshot(workspace)['episodes']
                           if item.get('open_work') or item.get('rationale') or item.get('goal'))
        return source, episode

    def brief(self, query='Continue RET-17', budget=6000):
        with Store(self.data) as store:
            brief = build_project_brief(store, self.project, query=query, budget=budget)
        self.assertLessEqual(len(brief['xml']), budget)
        return ElementTree.fromstring(brief['xml'])

    def test_complete_open_work_above_old_episode_cap_displaces_incidental_fields(self):
        obligation = ('RET-17 must keep the invoice correction pending: ' + 'retain the approval record ' * 28 + '. '
                      + 'RET-17 must preserve the signed invoice: ' + 'retain every invoice line ' * 28 + '. '
                      + 'RET-17 must wait for approval: ' + 'retain the review evidence ' * 28 + '.')
        _, episode = self.process_fields({'summary': 'Task RET-17 continues.',
            'goal': 'RET-17 is the invoice task.', 'open_work': obligation,
            'outcome': 'RET-17 local formatting finished.'}, incidental=4)
        tree = self.brief()
        fields = tree.findall("./item/field[@name='open_work_source_excerpt']")
        self.assertEqual(len(fields), 1, ElementTree.tostring(tree, encoding='unicode'))
        sources = SourceRegistry(tree).field_sources(fields[0])
        stored = episode['evidence']['open_work']
        self.assertEqual([source['quote'] for source in sources], [ref['quote'] for ref in stored])
        self.assertEqual(len(sources), 3)
        for pointer, ref in zip(sources, stored):
            self.assertEqual(pointer['source_id'], ref['source_id'])
            self.assertEqual(pointer['source_role'], 'user_intent')
            self.assertEqual(pointer['source_field'], 'body')
            self.assertEqual((pointer['start'], pointer['end']), (ref['start'], ref['end']))
            self.assertEqual(pointer['end'] - pointer['start'], len(pointer['quote']))
            self.assertFalse(pointer['quote_truncated'])
        self.assertIsNone(fields[0].find('derived_text'))

    def test_fitting_shared_reference_survives_omission_of_larger_group(self):
        goal = 'RET-17 must retain the original invoice approval.'
        obligation = goal + ' Preserve the audit detail ' + 'invoice correction evidence ' * 85 + '.'
        _, episode = self.process_fields({'summary': 'Task RET-17 continues.',
            'open_work': obligation, 'goal': goal})
        tree = self.brief(budget=2200)
        self.assertIsNone(tree.find("./item/field[@name='open_work_source_excerpt']"))
        fallback = tree.find("./item/field[@name='goal_source_excerpt']")
        self.assertIsNotNone(fallback, ElementTree.tostring(tree, encoding='unicode'))
        self.assertEqual([quote.text for quote in fallback.findall('./source/quote_excerpt')],
                         [ref['quote'] for ref in episode['evidence']['goal']])
        self.assertGreater(int(tree.find('item').get('fields_omitted', '0')), 0)

    def test_early_obligation_and_reason_survive_later_correction_and_large_outcome(self):
        obligation = 'RET-17 do not open a second ticket: ' + 'keep the first invoice task pending ' * 45 + '.'
        reason = 'RET-17 needs the approval trail because ' + 'the audit must retain its signed invoice evidence ' * 18 + '.'
        early_source, early = self.process_fields({'summary': 'Task RET-17 corrects invoices.',
            'goal': 'RET-17 needs one invoice ticket.', 'open_work': obligation, 'rationale': reason})
        correction = 'RET-17 correction: do not stop after formatting; approval still needs review.'
        late_source, current = self.process_fields({'summary': 'Continue task RET-17.',
            'open_work': correction, 'outcome': 'RET-17 formatted the invoice with ' + 'the original line spacing ' * 110 + '.'},
            session='later', continuation=early['id'])
        tree = self.brief()
        self.assertIn(correction, ''.join(tree.itertext()))
        historical = tree.find("./item/field[@name='prior_open_work_source_excerpt']")
        self.assertIsNotNone(historical, ElementTree.tostring(tree, encoding='unicode'))
        self.assertEqual([quote.text for quote in historical.findall('./source/quote_excerpt')],
                         [ref['quote'] for ref in current['evidence']['prior_open_work']])
        self.assertEqual(tree.find('item').get('prior_context_status'),
                         'historical_current_validity_unresolved')
        reasons = self.brief(query='Why RET-17?')
        retained_reason = reasons.find("./item/field[@name='prior_rationale_source_excerpt']")
        self.assertIsNotNone(retained_reason, ElementTree.tostring(reasons, encoding='unicode'))
        self.assertEqual([quote.text for quote in retained_reason.findall('./source/quote_excerpt')],
                         [ref['quote'] for ref in current['evidence']['prior_rationale']])
        self.assertIn(early_source['id'], ElementTree.tostring(tree, encoding='unicode'))
        self.assertIn(late_source['id'], ElementTree.tostring(tree, encoding='unicode'))

    def test_unfit_complete_group_has_explicit_omission_without_partial_quotes(self):
        obligation = 'RET-17 must preserve all qualifiers: ' + 'the invoice audit trail remains pending ' * 55 + '.'
        self.process_fields({'summary': 'Task RET-17 continues.', 'open_work': obligation})
        for budget in (128, 500, 1800):
            with self.subTest(budget=budget):
                tree = self.brief(budget=budget)
                self.assertIsNone(tree.find("./item/field[@name='open_work_source_excerpt']"))
                self.assertNotIn('the invoice audit trail', ''.join(tree.itertext()))
                if budget == 1800:
                    self.assertGreater(int(tree.find('item').get('fields_omitted', '0')), 0)

    def test_large_early_outcome_keeps_later_independent_constraint_and_reason(self):
        constraint = 'RET-17 keep the signed approval pending until the owner reviews it.'
        reason = 'RET-17 needs the original approval because it records the audit decision.'
        _, earlier = self.process_fields({'summary': 'Task RET-17 tracks invoice approval.',
            'open_work': constraint, 'rationale': reason}, session='approval')
        self.process_fields({'summary': 'Task RET-17 formatting is separate.',
            'goal': 'RET-17 format the local invoice.',
            'open_work': 'RET-17 formatting review remains pending.',
            'outcome': 'RET-17 formatted the invoice: ' + 'the original line spacing remains unchanged ' * 65 + '.'},
            session='formatting')
        tree = self.brief()
        text = ''.join(tree.itertext())
        self.assertIn(constraint, text, ElementTree.tostring(tree, encoding='unicode'))
        self.assertIn(reason, text, ElementTree.tostring(tree, encoding='unicode'))
        retained = tree.find("./item[@id='" + earlier['id'] + "']")
        self.assertIsNotNone(retained)
        self.assertIsNone(retained.get('prior_context_status'))
        for source in retained.findall('./field/source'):
            self.assertEqual(source.get('source_role'), 'user_intent')
            self.assertEqual(source.get('quote_truncated'), 'False')

    def test_consumed_omitted_user_source_is_visible_without_becoming_active_requirement(self):
        body = 'Do not deploy RET-17 before the owner approves the signed invoice.'
        with Store(self.data) as store:
            source = store.remember(self.project, 'Approval source', body,
                                    source='hook:UserPromptSubmit', session_id='approval')
        result = process_project_memory(self.project, self.data, runner=lambda request: receipt())
        self.assertEqual(result['status'], 'processed', result)
        with Store(self.data) as store:
            brief = build_project_brief(store, self.project, query='Continue RET-17')
        self.assertEqual(brief['claims'], [])
        tree = ElementTree.fromstring(brief['xml'])
        custody = tree.find("./item[@lane='intent_custody']")
        self.assertIsNotNone(custody, brief['xml'])
        self.assertEqual(custody.get('disposition'), 'unresolved')
        self.assertEqual(custody.get('is_requirement_claim'), 'False')
        self.assertIsNone(custody.get('active_requirement'))
        self.assertEqual(custody.get('meaning'), 'original_user_source_with_unreconciled_representation')
        self.assertEqual(custody.get('uncertainty'), 'extracted_representation_completeness_unresolved')
        pointer = custody.find('./field/source')
        self.assertEqual(pointer.get('source_id'), source['id'])
        self.assertEqual(pointer.get('source_role'), 'user_intent')
        self.assertEqual(pointer.findtext('quote_excerpt'), body)
        self.assertIsNone(custody.find('./field/derived_text'))
        self.assertEqual(tree.find('coverage').get('unresolved_user_sources'), '1')
        self.assertEqual(tree.find('coverage').get('pending_reconciliation'), 'true')
        with Store(self.data) as store:
            hidden = build_project_brief(store, self.project, query='Continue RET-17', exclude_session_id='approval')
            unrelated = build_project_brief(store, self.project, query='Continue RET-99')
        self.assertNotIn(body, hidden['xml'])
        self.assertNotIn(body, unrelated['xml'])
        self.assertLessEqual(len(brief['xml']), 6000)
        with Store(self.data) as store:
            startup = store.context(self.project, query='Continue RET-17', budget=6000,
                                    allow_remote=False, include_health=False)
        self.assertLessEqual(len(startup), 6000)
        startup_tree = ElementTree.fromstring(startup)
        self.assertIn(body, ''.join(startup_tree.itertext()))
        self.assertIsNotNone(startup_tree.find(".//item[@lane='intent_custody']"))

    def test_global_budget_counts_fields_and_records_without_partial_source_groups(self):
        self.process_fields({'summary': 'Task RET-17 continues.',
            'goal': 'RET-17 preserves the signed invoice.',
            'open_work': 'RET-17 must preserve all approval qualifiers: ' + 'invoice audit approval detail ' * 75 + '.'},
            incidental=4)
        for budget in (128, 1800, 6000):
            with self.subTest(budget=budget), Store(self.data) as store:
                brief = build_project_brief(store, self.project, query='Continue RET-17', budget=budget)
                tree = ElementTree.fromstring(brief['xml'])
                self.assertLessEqual(len(brief['xml']), budget)
                counts = brief['render_omissions']
                self.assertGreater(counts['fields'], 0)
                if budget == 128:
                    self.assertGreater(counts['records'], 0)
                else:
                    marker = tree.find('omitted')
                    self.assertIsNotNone(marker, brief['xml'])
                    self.assertEqual(int(marker.get('records')), counts['records'])
                    self.assertEqual(int(marker.get('fields')), counts['fields'])
                for pointer in tree.findall('./item/field/source'):
                    quote = pointer.findtext('quote_excerpt')
                    if quote is not None and pointer.get('quote_truncated') == 'False':
                        self.assertEqual(int(pointer.get('end')) - int(pointer.get('start')), len(quote))

    def test_same_task_id_in_foreign_project_never_supplies_retained_context(self):
        foreign = self.project.parent / 'foreign'
        foreign.mkdir()
        configure(self.data, project_memory_projects=[str(self.project), str(foreign)])
        foreign_source, _ = self.process_fields({'summary': 'Task RET-17 has foreign invoices.',
            'goal': 'RET-17 foreign only.', 'open_work': 'RET-17 foreign confidential invoice detail.'}, project=foreign)
        local_source, _ = self.process_fields({'summary': 'Task RET-17 has local invoices.',
            'goal': 'RET-17 local only.', 'open_work': 'RET-17 local approval remains pending.'})
        tree = self.brief()
        xml = ElementTree.tostring(tree, encoding='unicode')
        self.assertIn(local_source['id'], xml)
        self.assertIn('local approval remains pending', xml)
        self.assertNotIn(foreign_source['id'], xml)
        self.assertNotIn('foreign confidential', xml)
