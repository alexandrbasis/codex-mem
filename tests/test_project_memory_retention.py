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
        quotes = fields[0].findall('./source/quote_excerpt')
        self.assertEqual([quote.text for quote in quotes], [ref['quote'] for ref in episode['evidence']['open_work']])
        source = fields[0].find('source')
        self.assertEqual(source.get('source_role'), 'user_intent')
        self.assertEqual(len(quotes), 3)
        for pointer, quote in zip(fields[0].findall('source'), quotes):
            self.assertEqual(int(pointer.get('end')) - int(pointer.get('start')), len(quote.text))
            self.assertEqual(pointer.get('quote_truncated'), 'False')
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
