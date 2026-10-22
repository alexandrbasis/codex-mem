"""Complete selected originals at public build, render and Store context seams."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree as ET

from codex_mem.config import configure
from codex_mem.project_memory import ProjectMemory
from codex_mem.project_memory_retrieval import build_project_brief, render_project_brief
from codex_mem.store import Store
from codex_mem.tool_io import normalize_capture


class CompleteSourceGroupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / 'public-example'
        self.project.mkdir()
        self.store = Store(Path(temporary.name) / 'data')
        self.addCleanup(self.store.close)
        configure(self.store.data_dir, capture_scope='all', project_memory_mode='pilot',
                  project_memory_projects=[str(self.project)])

    def remember(self, text, source='hook:UserPromptSubmit', session='one'):
        return self.store.remember(self.project, 'Public source', text, source=source, session_id=session)

    def ref(self, row, start=0, end=None):
        end = len(row['body']) if end is None else end
        return {'source_id': row['id'], 'start': start, 'end': end, 'quote': row['body'][start:end]}

    def commit(self, episodes=(), claims=()):
        memory = ProjectMemory(self.store)
        result = memory.commit(memory.claim(self.project),
            {'episodes': list(episodes), 'claims': list(claims), 'relations': []})
        self.assertEqual('committed', result['status'], result)

    def populate_split_result(self):
        user = self.remember('Keep the receiver test open pending an outdoor check.')
        output = 'Bench sample: all 27 channels decoded. Outdoor reception has no recorded measurement.'
        tool = self.remember(output, source='hook:PostToolUse')
        split = output.index(' Outdoor')
        self.commit(episodes=[{'key': 'receiver', 'summary': output, 'goal': user['body'],
            'open_work': output[split:], 'outcome': output[:split],
            'evidence': {'summary': [self.ref(tool, 0, split), self.ref(tool, split)],
                         'goal': [self.ref(user)], 'open_work': [self.ref(tool, split)],
                         'outcome': [self.ref(tool, 0, split)]}}])
        return user, tool

    def test_complementary_material_intervals_survive_as_one_source_without_execution_claim(self):
        _, tool = self.populate_split_result()
        query = 'What did the captured receiver check establish?'
        brief = build_project_brief(self.store, self.project, query=query, budget=6000)
        for xml in (brief['xml'], self.store.context(self.project, query=query, budget=6000,
                         allow_remote=False, include_health=False)):
            tree = ET.fromstring(xml)
            sources = [s for s in tree.iter('source') if s.get('source_id') == tool['id']]
            full = [s for s in sources if s.findtext('quote_excerpt') == tool['body']]
            self.assertTrue(full, xml)
            self.assertEqual(('0', str(len(tool['body']))), (full[0].get('start'), full[0].get('end')))
            self.assertEqual('tool_record', full[0].get('source_role'))
            self.assertFalse(tree.findall('.//execution'), xml)
            self.assertIn('field_association="not_assessed"', xml)
            self.assertLessEqual(len(xml), 6000)

    def test_small_budget_keeps_complete_complementary_source_or_explicit_omissions(self):
        _, tool = self.populate_split_result()
        query = 'What did the captured receiver check establish?'
        medium = build_project_brief(self.store, self.project, query=query, budget=1400)
        self.assertIn(tool['body'], medium['xml'])
        self.assertLessEqual(len(medium['xml']), 1400)
        tiny = build_project_brief(self.store, self.project, query=query, budget=600)
        tree = ET.fromstring(tiny['xml'])
        self.assertIsNotNone(tree.find('omitted'))
        self.assertLessEqual(len(tiny['xml']), 600)
        for item in tree.findall("./item[@lane='source_excerpts']"):
            self.assertEqual(tool['body'], item.findtext('./field/source/quote_excerpt'))

    def test_source_groups_keep_session_deletion_and_unknown_topic_guards(self):
        _, tool = self.populate_split_result()
        query = 'What did the captured receiver check establish?'
        excluded = build_project_brief(self.store, self.project, query=query, exclude_session_id='one')
        self.assertNotIn(tool['id'], excluded['xml'])
        unrelated = build_project_brief(self.store, self.project,
            query='What did the captured iridescent telescope check establish?')
        self.assertNotIn(tool['id'], unrelated['xml'])
        self.store.forget(self.project, [tool['id']])
        self.assertNotIn(tool['id'], build_project_brief(self.store, self.project, query=query)['xml'])

    def test_distinct_sources_and_gaps_are_never_assembled_as_one_quote(self):
        first = self.remember('Accepted bench sample. Unrelated middle. Pending field test.', source='hook:PostToolUse')
        second = self.remember('A different device needs its own test.', source='hook:PostToolUse')
        left = self.ref(first, 0, len('Accepted bench sample.'))
        right = self.ref(first, first['body'].index(' Pending'))
        self.commit(episodes=[{'key': 'checks', 'summary': first['body'], 'outcome': left['quote'],
            'open_work': right['quote'] + second['body'], 'evidence': {'summary': [left, right],
                'outcome': [left], 'open_work': [right, self.ref(second)]}}])
        tree = ET.fromstring(build_project_brief(self.store, self.project,
            query='What did the captured bench check establish?')['xml'])
        for source in tree.iter('source'):
            quote = source.findtext('quote_excerpt')
            if quote is not None:
                row = first if source.get('source_id') == first['id'] else second
                self.assertEqual(row['body'][int(source.get('start')):int(source.get('start')) + len(quote)], quote)
        self.assertTrue(tree.findall('.//source'))
        self.assertNotIn(left['quote'] + right['quote'], [source.findtext('quote_excerpt') for source in tree.iter('source')])

    def test_exact_assembled_value_saves_prose_but_keeps_typed_claim_and_literal_difference(self):
        body = 'Preserve the case label exactly. Preserve the final separator exactly.'
        row = self.remember(body)
        split = body.index(' Preserve')
        self.commit(claims=[{'key': 'case-label', 'kind': 'requirement', 'workflow_state': 'open',
            'text': body, 'scope': {'environment': 'local', 'version': '4.6.2'},
            'evidence': {'text': [self.ref(row, 0, split), self.ref(row, split)]}}])
        brief = build_project_brief(self.store, self.project, query='Which requirements remain?')
        record = next(r for r in brief['records'] if r['lane'] == 'claim')
        compact = render_project_brief({'records': [record], 'coverage': {}}, budget=6000)
        tree = ET.fromstring(compact)
        claim = tree.find('item')
        self.assertEqual('open', claim.get('workflow_state'))
        self.assertEqual(('local', '4.6.2'), (claim.get('environment'), claim.get('version')))
        self.assertIsNone(claim.find('./field/derived_text'))
        self.assertEqual(body, ''.join(claim.itertext()))
        distinct = deepcopy(record)
        distinct['fields'][0]['text'] = body + ' Do not normalize spaces.'
        expanded = render_project_brief({'records': [distinct], 'coverage': {}}, budget=6000)
        self.assertIn(distinct['fields'][0]['text'], expanded)
        self.assertGreater(len(expanded) - len(compact), len(body))

    def test_source_groups_keep_per_container_scope_uncertainty_and_unique_identity(self):
        def episode(identifier, scope, uncertainty):
            body = 'Accepted sample. Further inspection pending.'
            split = body.index(' Further')
            return {'id': identifier, 'lane': 'episode', 'scope': {'environment': scope},
                'uncertainty': uncertainty, 'fields': [
                    {'name': name, 'text': '', 'basis': 'attributed_source_excerpts',
                     'field_association': 'model_inference', 'evidence': [{'source_id': identifier,
                        'source_role': 'tool_record', 'start': start, 'end': end,
                        'quote_excerpt': body[start:end], 'quote_truncated': False}]}
                    for name, start, end in [('outcome', 0, split), ('open_work', split, len(body))]]}
        records = [episode('local-source', 'local', 'limited_to_bench'),
                   episode('production-source', 'production', 'scope_conflict'),
                   episode('other-local-source', 'local', 'limited_to_bench')]
        xml = render_project_brief({'records': records, 'coverage': {},
            'source_evidence_requested': True}, budget=6000)
        tree = ET.fromstring(xml)
        groups = tree.findall("./item[@lane='source_excerpts']")
        self.assertEqual(2, len(groups), xml)
        self.assertEqual(len(groups), len({item.get('id') for item in groups}))
        for item in groups:
            expected = {'local': ('limited_to_bench', {'local-source', 'other-local-source'}),
                        'production': ('scope_conflict', {'production-source'})}[item.get('environment')]
            self.assertEqual(expected[0], item.get('uncertainty'))
            self.assertEqual(expected[1], {source.get('source_id') for source in item.iter('source')})

    def test_unknown_tool_capture_is_not_promoted_to_execution_without_material_selection(self):
        output = 'Bench sample: all 27 channels decoded. Outdoor reception not tested.'
        capture = normalize_capture({'tool_name': 'custom_probe.read', 'tool_use_id': 'public-probe',
            'session_id': 'one', 'tool_input': {'cmd': 'receiver sample'},
            'tool_response': {'exit_code': 0, 'output': output}}, project=self.project)
        row = self.store.remember(self.project, 'Custom tool', output, source='hook:PostToolUse',
                                  session_id='one', tool_capture=capture)
        memory = ProjectMemory(self.store)
        memory.commit(memory.claim(self.project), {'claims': [], 'episodes': [], 'relations': []})
        xml = build_project_brief(self.store, self.project,
            query='What did the captured receiver check establish?')['xml']
        self.assertNotIn(row['id'], xml)
        self.assertNotIn('<execution', xml)

    def test_ordinary_source_plurals_match_but_quoted_literals_remain_exact(self):
        captions = self.remember('I prepared the captions.', source='hook:Stop')
        slip = self.remember('I prepared the slip.', source='hook:Stop')
        self.commit()
        for query, row in [('What proves whether the caption draft exists?', captions),
                           ('Was the slips draft established?', slip)]:
            with self.subTest(query=query):
                xml = build_project_brief(self.store, self.project, query=query)['xml']
                self.assertIn(row['id'], xml)
                self.assertIn(row['body'], xml)
        exact = build_project_brief(self.store, self.project,
            query='What proves whether the `caption` draft exists?')
        self.assertNotIn(captions['id'], exact['xml'])

    def test_generic_requirement_frames_keep_obligations_and_residual_subjects(self):
        row = self.remember('Ask the operator to inspect the receiver. Leave dispatch open.')
        self.commit()
        for query in ('Which unfinished steps remain after the continuation?',
                      'What is authorized now and what remains unfinished?'):
            with self.subTest(query=query):
                self.assertIn(row['body'], build_project_brief(self.store, self.project, query=query)['xml'])
        for query in ('Which telescope steps remain after the continuation?',
                      'What does the iridescent-telescope continuation require?'):
            with self.subTest(query=query):
                self.assertNotIn(row['id'], build_project_brief(self.store, self.project, query=query)['xml'])

    def test_original_adjective_is_current_content_but_explicit_past_queries_stay_historical(self):
        from codex_mem.project_memory_query import plan_query
        row = self.remember('Keep the original donor codes. The paper index depends on their spelling.')
        self.commit()
        query = 'Where must original donor codes remain?'
        brief = build_project_brief(self.store, self.project, query=query)
        self.assertNotEqual('historical_query', brief.get('skip_reason'))
        self.assertIn(row['body'], brief['xml'])
        for query in ('What was the original state?', 'What originally governed the donor codes?',
                      'What was known as-of version 4.6.2?'):
            self.assertTrue(plan_query(query, self.project).historical)


if __name__ == '__main__':
    unittest.main()
