"""Whole admitted sources use available bytes without changing their meaning."""
from copy import deepcopy
import unittest
from xml.etree import ElementTree as ET

from codex_mem.project_memory_retrieval import render_project_brief


def pointer(source_id, text, seq):
    return {'source_id': source_id, 'source_role': 'user_intent', 'source_field': 'body',
            'start': 0, 'end': len(text), 'quote_excerpt': text, 'quote_truncated': False,
            'source_capture_seq': seq}


def neutral(source_id, text, seq):
    return {'id': source_id, 'lane': 'intent_custody', 'source_role': 'user_intent',
            'source_capture_seq_min': seq, 'source_capture_seq_max': seq,
            'selection_basis': 'unassociated_user_context', 'relationship_to_query': 'unresolved',
            'is_requirement_claim': False, 'disposition': 'stored',
            'meaning': 'original_user_observation', 'uncertainty': 'current_applicability_unresolved',
            'fields': [{'name': 'source_custody', 'basis': 'attributed_source_excerpts',
                        'evidence': [pointer(source_id, text, seq)]}]}


class WholeSourceAllocationTests(unittest.TestCase):
    def test_four_independent_neutral_sources_fit_without_a_count_cutoff(self):
        records = [neutral('source-a', 'Retain the green cover.', 1),
                   neutral('source-b', 'Keep the linen sample.', 2),
                   neutral('source-c', 'Reopen the catalogue to inspect its binding.', 3),
                   neutral('source-d', 'The parcel label needs review.', 4)]
        brief = {'records': [], 'neutral_original_context': True, 'unassociated_user_context': records}
        before = deepcopy(brief)
        xml = render_project_brief(brief, budget=6000)
        tree = ET.fromstring(xml)
        for record in records:
            ref = record['fields'][0]['evidence'][0]
            sources = [node for node in tree.iter('source') if node.get('source_id') == ref['source_id']]
            self.assertTrue(any(node.findtext('quote_excerpt') == ref['quote_excerpt'] and
                node.get('start') == '0' and node.get('end') == str(ref['end']) and
                node.get('source_role') == 'user_intent' and node.get('source_field') == 'body'
                for node in sources), xml)
        for item in tree.findall('item'):
            self.assertEqual('unresolved', item.get('relationship_to_query'))
            self.assertEqual('False', item.get('is_requirement_claim'))
            self.assertEqual('current_applicability_unresolved', item.get('uncertainty'))
            self.assertIsNone(item.get('workflow_state'))
        self.assertEqual('4', tree.find('source-context-coverage').get('selected_source_count'))
        self.assertEqual('0', tree.find('source-context-coverage').get('omitted'))
        self.assertLessEqual(len(xml), 1800)
        self.assertEqual(before, brief)

    def test_selected_current_original_precedes_optional_neutral_groups_at_owner_budget(self):
        body = ('Require signed authorization before cutting the fabric. ' +
                'Retain the independent inspection requirement. ' * 30)[:1190]
        selected = {'id': 'authority-episode', 'lane': 'episode',
                    'is_current_head': True, 'episode_version': 2,
                    'fields': [{'name': 'goal', 'basis': 'attributed_source_excerpts',
                                'evidence': [pointer('authority-source', body, 1)]}]}
        report_text = 'The local sample is recorded. No authority was granted.'
        report = {'id': 'sample-report', 'lane': 'source_evidence',
                  'source_role': 'assistant_report', 'fields': [{'name': 'body',
                      'evidence': [{**pointer('sample-report', report_text, 2),
                                    'source_role': 'assistant_report'}]}]}
        brief = {'records': [selected], 'neutral_original_context': True,
                 'unassociated_role_context': [report],
                 'unassociated_user_context': [neutral('other-' + str(index),
                    'Keep this independent item available for inspection. ' + str(index), index + 3)
                    for index in range(4)]}
        before = deepcopy(brief)
        for budget in (2900, 6000):
            xml = render_project_brief(brief, budget=budget)
            tree = ET.fromstring(xml)
            self.assertTrue(any(node.get('source_id') == 'authority-source' and
                node.findtext('quote_excerpt') == body and node.get('source_role') == 'user_intent' and
                node.get('source_field') == 'body' and node.get('start') == '0' and
                node.get('end') == str(len(body)) for node in tree.iter('source')), xml)
            self.assertTrue(any(node.get('source_id') == 'sample-report' and
                node.findtext('quote_excerpt') == report_text and
                node.get('source_role') == 'assistant_report' for node in tree.iter('source')), xml)
            for item in tree.findall("item[@selection_basis='unassociated_user_context']"):
                self.assertEqual('unresolved', item.get('relationship_to_query'))
                self.assertEqual('False', item.get('is_requirement_claim'))
                self.assertIsNone(item.get('workflow_state'))
            self.assertLessEqual(len(xml), budget)
        self.assertEqual(before, brief)


    def test_public_brief_reports_candidate_bound_instead_of_obsolete_count_limit(self):
        import tempfile
        from pathlib import Path
        from codex_mem.config import configure
        from codex_mem.project_memory import ProjectMemory
        from codex_mem.project_memory_retrieval import build_project_brief
        from codex_mem.store import Store

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'workspace'
            with Store(Path(directory) / 'data') as store:
                configure(store.data_dir, project_memory_mode='pilot', capture_scope='all',
                          project_memory_projects=[str(project)])
                originals = []
                for index in range(4):
                    row = store.remember(project, 'Independent observation',
                        f'Keep independent sample {index} for inspection.',
                        source='hook:UserPromptSubmit', session_id=str(index))
                    originals.append(row)
                    memory = ProjectMemory(store)
                    memory.commit(memory.claim(project), {'episodes': [], 'claims': [], 'relations': []})
                brief = build_project_brief(store, project,
                    query='What is known about the telescope arrangement?', budget=6000)
                coverage = brief['unassociated_user_context_coverage']
                self.assertEqual(4, coverage['returned_source_limit'])
                self.assertEqual(4, coverage['selected_source_count'])
                self.assertEqual(0, coverage['omitted'])
                tree = ET.fromstring(brief['xml'])
                self.assertEqual('4', tree.find('source-context-coverage').get('selected_source_count'))
                for original in originals:
                    self.assertTrue(any(source.get('source_id') == original['id'] and
                        source.findtext('quote_excerpt') == original['body'] for source in tree.iter('source')))
                self.assertLessEqual(len(brief['xml']), 6000)
                for index in range(4, 140):
                    store.remember(project, 'Independent observation',
                        f'Keep independent sample {index} for inspection.',
                        source='hook:UserPromptSubmit', session_id=str(index))
                    memory.commit(memory.claim(project), {'episodes': [], 'claims': [], 'relations': []})
                bounded = build_project_brief(store, project,
                    query='What is known about the telescope arrangement?', budget=6000)
                self.assertLessEqual(len(bounded['unassociated_user_context']), 116)
                self.assertLessEqual(bounded['unassociated_user_context_coverage']['returned_source_limit'], 116)
                self.assertGreater(bounded['unassociated_user_context_coverage']['omitted'], 0)
                self.assertLessEqual(len(bounded['xml']), 6000)
                self.assertGreater(bounded['coverage']['user_sources_omitted'], 0)


    def test_exact_escaped_whole_group_cost_skips_large_candidate_and_uses_remaining_room(self):
        import re
        large = neutral('large-original', 'A<&>' * 140, 4)
        small = [neutral('small-' + str(index), f'Retain sample {index}.', index + 1)
                 for index in range(3)]
        brief = {'records': [], 'neutral_original_context': True,
                 'unassociated_user_context': small + [large]}
        for budget in (2900, 6000):
            xml = render_project_brief(brief, budget=budget)
            tree = ET.fromstring(xml)
            self.assertFalse(any(source.get('source_id') == 'large-original' for source in tree.iter('source')))
            expected = small[:2] if budget == 2900 else small
            for record in expected:
                ref = record['fields'][0]['evidence'][0]
                self.assertTrue(any(source.get('source_id') == record['id'] and
                    source.findtext('quote_excerpt') == ref['quote_excerpt'] for source in tree.iter('source')), xml)
            groups = re.findall(r'<item\b[^>]*selection_basis="unassociated_user_context"[^>]*>.*?</item>', xml)
            marker = re.search(r'<source-context-coverage\b[^>]*/>', xml).group()
            self.assertLessEqual(sum(map(len, groups)) + len(marker), min(1800, budget // 3))
            self.assertEqual(str(len(expected)), tree.find('source-context-coverage').get('selected_source_count'))
            self.assertEqual(str(4 - len(expected)), tree.find('source-context-coverage').get('omitted'))
            self.assertLessEqual(len(xml), budget)


    def test_selected_open_task_association_precedes_optional_neutral_transport(self):
        body = ('Inspect the seal before release. ' + 'Retain the original inspection notes. ' * 40)[:1000]
        claim = {'id': 'selected-task', 'lane': 'claim', 'source_role': 'user_intent',
                 'project_memory_kind': 'task', 'workflow_state': 'open', 'evidence_basis': 'exact_quote',
                 'fields': [{'name': 'text', 'text': body, 'basis': 'attributed_source_excerpts',
                             'evidence': [pointer('task-source', body, 1)]}]}
        report_text = 'The local sample is recorded. No authority was granted.'
        report = {'id': 'sample-report', 'lane': 'source_evidence',
                  'source_role': 'assistant_report', 'fields': [{'name': 'body',
                      'evidence': [{**pointer('sample-report', report_text, 2),
                                    'source_role': 'assistant_report'}]}]}
        brief = {'records': [claim], 'neutral_original_context': True,
                 'unassociated_role_context': [report],
                 'unassociated_user_context': [neutral('other-' + str(index),
                    'Keep this independent item available for inspection. ' + str(index), index + 3)
                    for index in range(4)]}
        xml = render_project_brief(brief, budget=2900)
        tree = ET.fromstring(xml)
        task = tree.find("item[@id='selected-task']")
        self.assertIsNotNone(task, xml)
        self.assertEqual('open', task.get('workflow_state'))
        self.assertEqual('task', task.get('project_memory_kind'))
        self.assertIsNotNone(task.find("field[@name='text']"))
        self.assertTrue(any(source.get('source_id') == 'task-source' and
            source.findtext('quote_excerpt') == body for source in tree.iter('source')), xml)
        self.assertLessEqual(len(xml), 2900)
        spacious = ET.fromstring(render_project_brief(brief, budget=6000))
        self.assertEqual(1, len(spacious.findall("item[@id='selected-task']")))



if __name__ == '__main__':
    unittest.main()
