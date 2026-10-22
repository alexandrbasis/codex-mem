"""Bounded source coverage through real Stores and the public context interface."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

from codex_mem.config import configure
from codex_mem.project_memory import ProjectMemory
from codex_mem.project_memory_query import plan_query
from codex_mem.project_memory_retrieval import build_project_brief
from codex_mem.store import Store
from codex_mem.tool_io import normalize_capture


class SourceCoverageAllocationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name) / 'public-radio'
        self.project.mkdir()
        self.other = Path(temp.name) / 'other-task'
        self.other.mkdir()
        self.store = Store(Path(temp.name) / 'data')
        self.addCleanup(self.store.close)
        configure(self.store.data_dir, capture_scope='all', project_memory_mode='pilot',
                  project_memory_projects=[str(self.project), str(self.other)])

    def user(self, text, session='user', project=None):
        return self.store.remember(project or self.project, 'Public user source', text,
            source='hook:UserPromptSubmit', session_id=session)

    def tool(self, text, index, code=0):
        capture = normalize_capture({'tool_name': 'functions.exec_command', 'session_id': 'tools',
            'tool_use_id': f'capture-{index}', 'tool_input': {'cmd': f'python check.py ORB-27 --run {index}'},
            'tool_response': {'exit_code': code, 'output': text}}, project=self.project)
        return self.store.remember(self.project, 'ORB-27 actual check', text,
            source=f'hook:PostToolUse:capture-{index}', session_id='tools', tool_capture=capture)

    @staticmethod
    def span(row):
        return {'source_id': row['id'], 'start': 0, 'end': len(row['body']), 'quote': row['body']}

    def consume(self, row, *, claim=False, environment='', version=''):
        memory = ProjectMemory(self.store)
        snapshot = memory.claim(self.project)
        ref = self.span(row)
        record = {'key': 'public', 'summary': row['body'], 'evidence': {'summary': [ref]}}
        claims = [{'key': 'obligation', 'text': row['body'], 'kind': 'requirement',
            'workflow_state': 'open', 'scope': {'environment': environment, 'version': version},
            'evidence': {'text': [ref], 'workflow_state': [ref]}}] if claim else []
        result = memory.commit(snapshot, {'episodes': [] if environment or version else [record], 'claims': claims, 'relations': []})
        self.assertEqual(result['status'], 'committed')

    @staticmethod
    def visible(xml, source):
        return [node for node in ET.fromstring(xml).iter('source') if node.get('source_id') == source['id']
                and node.findtext('quote_excerpt') == source['body']]

    def test_latest_named_user_source_survives_tools_and_old_obligation_pressure(self):
        old = self.user('ORB-27 requires a verbose inspection worksheet. ' + 'Retain the instrument history. ' * 12)
        self.consume(old, claim=True)
        for index in range(2):
            tool = self.tool('ORB-27 local sample. ' + 'The observed carrier was stable. ' * 22, index)
            memory = ProjectMemory(self.store)
            memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        self.store.remember(self.project, 'Assistant report', 'ORB-27 inspection was approved. ' * 12,
                            source='hook:Stop', session_id='report')
        memory = ProjectMemory(self.store)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        current = self.user('ORB-27: leave out the inspection worksheet. Keep the calibration log because technicians compare drift.', 'correction')
        self.consume(current)
        unrelated = self.user('OTHER-91 needs a shipping manifest immediately.', 'unrelated')
        self.consume(unrelated)
        for budget in (2900, 6000):
            brief = build_project_brief(self.store, self.project, query='What requirements apply to ORB-27?', budget=budget)
            self.assertTrue(self.visible(brief['xml'], current), brief['xml'])
            self.assertNotIn(unrelated['id'], brief['xml'])
            self.assertLessEqual(len(brief['xml']), budget)
            for item in ET.fromstring(brief['xml']).findall("./item[@lane='source_evidence']"):
                if item.find('execution') is not None:
                    fields = {s.get('source_field') for s in item.iter('source')}
                    self.assertIn('tool_input.command', fields)
                    self.assertIn('tool_response.output', fields)
        context = self.store.context(self.project, query='What requirements apply to ORB-27?', budget=6000,
                                     allow_remote=False, include_health=False)
        self.assertTrue(self.visible(context, current), context)

    def test_ordinal_execution_window_reads_earliest_before_newest_cap(self):
        first = self.tool('ORB-27 alignment check failed: initial offset was excessive.', 0, 1)
        for index in range(1, 20):
            self.tool('ORB-27 alignment check passed: offset within tolerance.', index)
        memory = ProjectMemory(self.store)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        self.user('Yes, proceed only after approval.', 'unassociated-followup')
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        query = 'What did the first ORB-27 check establish?'
        plan = replace(plan_query(query, self.project), answer_subject='check',
                       answer_facets=('execution_assessment',), source_order='earliest')
        with patch('codex_mem.project_memory_query.plan_query', return_value=plan):
            brief = build_project_brief(self.store, self.project, query=query)
            self.assertTrue(self.visible(brief['xml'], first), brief['xml'])
            tools = [r for r in brief['source_evidence'] if r.get('execution')]
            self.assertEqual(tools[0]['id'], first['id'])
            self.assertEqual(brief['unassociated_user_context'], [])
            self.assertEqual(brief['unassociated_user_context_coverage']['selected_source_count'], 0)
            self.assertEqual(brief['source_evidence_coverage']['source_order'], 'earliest')
            self.assertLessEqual(brief['source_evidence_coverage']['roles']['tool_record']['candidates'], 16)

    def test_normalized_answer_subject_is_soft_query_not_original_facet(self):
        current = self.user('The printable ORB-27 receipt can wait.', 'revision')
        self.consume(current)
        query = 'Which requested ORB-27 output is deferred?'
        plan = replace(plan_query(query, self.project), answer_subject='', answer_facets=('requirements',))
        with patch('codex_mem.project_memory_query.plan_query', return_value=plan):
            brief = build_project_brief(self.store, self.project, query=query)
        self.assertTrue(self.visible(brief['xml'], current), brief['xml'])

    def test_named_soft_subject_does_not_reserve_newer_unrelated_user_source(self):
        wanted = self.user('The telescope calibration printout can wait.', 'telescope')
        self.consume(wanted)
        unrelated = self.user('The microscope schematic must be printed now.', 'microscope')
        self.consume(unrelated)
        query = 'Which requested telescope output is deferred?'
        brief = build_project_brief(self.store, self.project, query=query, budget=2900)
        self.assertTrue(self.visible(brief['xml'], wanted), brief['xml'])
        self.assertNotIn(unrelated['id'], brief['xml'])
        self.assertLessEqual(len(brief['xml']), 2900)

    def test_hard_task_anchor_cannot_replace_nonempty_soft_topic(self):
        wanted = self.user('ORB-27 telescope calibration printout can wait.', 'telescope')
        self.consume(wanted)
        unrelated = self.user('ORB-27 microscope schematic must be printed now.', 'microscope')
        self.consume(unrelated)
        query = 'Which requested ORB-27 telescope output is deferred?'
        plan = replace(plan_query(query, self.project), candidate_queries=('ORB-27',),
                       answer_subject='telescope', answer_facets=('requirements',))
        with patch('codex_mem.project_memory_query.plan_query', return_value=plan):
            brief = build_project_brief(self.store, self.project, query=query, budget=2900)
        self.assertTrue(self.visible(brief['xml'], wanted), brief['xml'])
        self.assertNotIn(unrelated['id'], brief['xml'])

    def test_every_displayed_user_excerpt_retains_exact_original_interval(self):
        sources = [self.user('ORB-27 requires a calibration log. Preserve its original units.', 'one')]
        self.consume(sources[0])
        sources.append(self.user('ORB-27: omit the paper chart. Keep raw readings because reviewers compare drift.', 'two'))
        self.consume(sources[1])
        originals = {row['id']: row['body'] for row in sources}
        for budget in (600, 1400, 2900, 6000):
            brief = build_project_brief(self.store, self.project, query='What requirements apply to ORB-27?', budget=budget)
            tree = ET.fromstring(brief['xml'])
            for source in tree.iter('source'):
                quote = source.findtext('quote_excerpt')
                if quote is not None:
                    body = originals[source.get('source_id')]
                    self.assertEqual(body[int(source.get('start')):int(source.get('end'))], quote)
            self.assertLessEqual(len(brief['xml']), budget)
            self.assertEqual(tree.get('freshness'), 'not_verified')

    def test_pronoun_and_unrelated_context_are_neutral_after_topical_material(self):
        wanted = self.user('The telescope export needs a review.', 'initial')
        self.consume(wanted)
        followup = self.user('Yes, keep CSV only until approval.', 'followup')
        memory = ProjectMemory(self.store)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        other = self.user('The microscope chart needs a new border.', 'different')
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        query = 'Which telescope export remains required?'
        plan = replace(plan_query(query, self.project), candidate_queries=('',),
                       answer_subject='telescope', answer_facets=('requirements',))
        with patch('codex_mem.project_memory_query.plan_query', return_value=plan):
            brief = build_project_brief(self.store, self.project, query=query, budget=6000)
            small = build_project_brief(self.store, self.project, query=query, budget=1400)
        self.assertTrue(self.visible(brief['xml'], wanted))
        self.assertTrue(self.visible(brief['xml'], followup), brief['xml'])
        self.assertTrue(self.visible(small['xml'], wanted), small['xml'])
        self.assertLessEqual(len(small['xml']), 1400)
        self.assertEqual(small['unassociated_user_context_coverage']['selected_source_count'], 0)
        self.assertEqual(small['unassociated_user_context_coverage']['omitted'], 2)
        tree = ET.fromstring(brief['xml'])
        for row in (followup, other):
            wrappers = [item for item in tree.findall('item') if any(
                source.get('source_id') == row['id'] for source in item.iter('source'))]
            self.assertTrue(wrappers)
            for item in wrappers:
                self.assertEqual(item.get('selection_basis'), 'unassociated_user_context')
                self.assertEqual(item.get('relationship_to_query'), 'unresolved')
                self.assertEqual(item.get('is_requirement_claim'), 'False')
                self.assertEqual(item.get('meaning'), 'original_user_source_with_unreconciled_representation')
                self.assertEqual(item.get('uncertainty'), 'extracted_representation_completeness_unresolved')
                self.assertIsNone(item.get('workflow_state'))
        coverage = brief['unassociated_user_context_coverage']
        self.assertEqual(coverage['selected_source_count'], 2)
        self.assertEqual(coverage['omitted'], 0)
        self.assertEqual(brief['relations'], [])
        unknown_plan = replace(plan, answer_subject='iridescent')
        with patch('codex_mem.project_memory_query.plan_query', return_value=unknown_plan):
            unknown = build_project_brief(self.store, self.project, query='Which iridescent export remains?')
        self.assertNotIn(followup['id'], unknown['xml'])

    def test_topical_snapshot_does_not_hide_recent_neutral_followups(self):
        wanted = self.user('The telescope export needs a review.', 'initial')
        self.consume(wanted)
        followup = self.user('Yes, keep CSV only until approval.', 'followup')
        memory = ProjectMemory(self.store)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        other = self.user('The microscope chart needs a new border.', 'different')
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        topical = memory.snapshot(self.project, query='telescope', signal_limit=8)
        scalar = memory.snapshot(self.project, query='', signal_limit=8)
        self.assertEqual(len(topical['intent_custody']), 1)
        self.assertEqual(len(scalar['intent_custody']), 3)
        with patch.object(self.store, 'get', wraps=self.store.get) as source_reads:
            brief = build_project_brief(self.store, self.project,
                                      query='Which telescope export remains required?', budget=6000)
        fetched = {source_id for call in source_reads.call_args_list for source_id in call.args[1]}
        self.assertNotIn(followup['id'], fetched)
        self.assertNotIn(other['id'], fetched)
        self.assertTrue(self.visible(brief['xml'], wanted), brief['xml'])
        for row in (followup, other):
            self.assertTrue(self.visible(brief['xml'], row), brief['xml'])
            wrappers = [item for item in ET.fromstring(brief['xml']).findall('item')
                        if any(ref.get('source_id') == row['id'] for ref in item.iter('source'))]
            self.assertTrue(all(item.get('relationship_to_query') == 'unresolved' and
                                item.get('selection_basis') == 'unassociated_user_context' and
                                item.get('is_requirement_claim') == 'False' for item in wrappers))
        unknown = build_project_brief(self.store, self.project, query='Which iridescent export remains required?')
        self.assertNotIn(followup['id'], unknown['xml'])

    def test_partial_soft_miss_custody_cannot_become_whole_source_supplement(self):
        wanted = self.user('The telescope export needs review.', 'topical')
        self.consume(wanted)
        body = 'Microscope chart annotations need review. ' * 12
        partial = self.store.remember(self.project, 'Telescope export transcript', body,
            source='hook:UserPromptSubmit', session_id='fragment')
        memory = ProjectMemory(self.store)
        result = memory.commit(memory.claim(self.project, max_chars=256),
                               {'episodes': [], 'claims': [], 'relations': []})
        self.assertEqual(result['status'], 'committed')
        state = memory.snapshot(self.project, query='telescope')
        interval = next(item for item in state['intent_custody'] if item['source_id'] == partial['id'])
        self.assertEqual((interval['start'], interval['end']), (0, 256))
        before = self.store.get(self.project, [partial['id']])[0]
        brief = build_project_brief(self.store, self.project,
                                  query='Which telescope export remains required?', budget=6000)
        self.assertTrue(self.visible(brief['xml'], wanted))
        self.assertNotIn(partial['id'], brief['xml'], brief['xml'])
        self.assertEqual(brief['unassociated_user_context_coverage']['selected_source_count'], 0)
        self.assertEqual(self.store.get(self.project, [partial['id']])[0], before)
        self.assertEqual(next(item for item in memory.snapshot(self.project)['intent_custody']
                              if item['source_id'] == partial['id'])['disposition'], 'unresolved')
        self.store.forget(self.project, [partial['id']])
        self.assertNotIn(partial['id'], {item['source_id'] for item in memory.snapshot(self.project)['intent_custody']})
        deleted = build_project_brief(self.store, self.project,
                                    query='Which telescope export remains required?', budget=6000)
        self.assertNotIn(partial['id'], deleted['xml'])

    def test_recent_custody_window_is_bounded_whole_unresolved_and_default_unchanged(self):
        from codex_mem.project_memory_custody import snapshot as custody_snapshot
        rows = []
        memory = ProjectMemory(self.store)
        for index in range(20):
            row = self.user(f'Public observation number {index}.', f'capture-{index}')
            rows.append(row)
            memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        represented = self.user('Preserve the complete public manifest.', 'represented')
        self.consume(represented, claim=True)
        large = self.user('A long neutral user source. ' * 30, 'large')
        self.consume(large)
        with self.store._lock:
            default, _ = custody_snapshot(self.store._connection, str(self.project.resolve()), 2)
            recent, coverage = custody_snapshot(self.store._connection, str(self.project.resolve()), 16,
                                               recent_unresolved=True)
            excluded, _ = custody_snapshot(self.store._connection, str(self.project.resolve()), 16,
                                            'capture-19', recent_unresolved=True)
        self.assertEqual([row['source_id'] for row in default], [row['id'] for row in rows[:2]])
        self.assertEqual([row['source_id'] for row in recent], [row['id'] for row in reversed(rows[4:])])
        self.assertEqual(coverage['matched'], 20)
        self.assertEqual(coverage['matched_omitted'], 4)
        self.assertEqual(coverage['selection_mode'], 'recent_unresolved_whole_sources')
        self.assertNotIn(rows[-1]['id'], {row['source_id'] for row in excluded})
        for item in recent:
            self.assertEqual(item['text'], item['source_record']['body'])
            self.assertEqual(item['start'], 0)
            self.assertEqual(item['end'], len(item['text']))
            self.assertLessEqual(len(item['text']), 600)

    def test_execution_facet_inflection_window_keeps_earliest_without_all_terms(self):
        first = self.tool('ORB-27 order check failed: the receipt changed after an edit.', 0, 1)
        for index in range(1, 20):
            self.tool('ORB-27 order check passed: receipts stayed stable.', index)
        memory = ProjectMemory(self.store)
        memory.commit(memory.claim(self.project), {'episodes': [], 'claims': [], 'relations': []})
        brief = build_project_brief(self.store, self.project,
            query='How should the first ordering check be recorded?', budget=6000)
        self.assertIn(first['id'], brief['xml'], brief['xml'])
        self.assertIn('order check failed', brief['xml'])
        self.assertEqual(brief['source_order'], 'earliest')
        self.assertLessEqual(len(brief['xml']), 6000)

    def test_declared_version_is_not_widened_by_original_user_coverage(self):
        row = self.user('ORB-27 calibration log belongs to version 1.2.0.', 'scoped')
        self.consume(row, claim=True, version='1.2.0')
        query = 'What requirements apply to ORB-27?'
        correct = build_project_brief(self.store, self.project, query=query, version='1.2.0')
        wrong = build_project_brief(self.store, self.project, query=query, version='1.2.1')
        self.assertIn(row['id'], correct['xml'])
        self.assertNotIn(row['id'], wrong['xml'])

    def test_named_topic_and_hard_source_guards_survive_user_reservation(self):
        wanted = self.user('ORB-27 calibration log stays local.', 'keep')
        self.consume(wanted, claim=True, environment='local')
        foreign = self.user('ORB-27 secret foreign instruction.', 'other', self.other)
        hidden = self.user('ORB-27 excluded current session.', 'hidden')
        self.consume(hidden)
        private = self.user('<private>ORB-27 concealed calibration.</private>', 'private')
        query = 'What requirements apply to ORB-27?'
        excluded = build_project_brief(self.store, self.project, query=query, exclude_session_id='hidden')
        self.assertTrue(self.visible(excluded['xml'], wanted))
        self.assertNotIn(hidden['id'], excluded['xml'])
        self.assertNotIn(foreign['id'], excluded['xml'])
        self.assertNotIn(private['id'], excluded['xml'])
        wrong_env = build_project_brief(self.store, self.project, query=query, environment='production')
        self.assertNotIn(wanted['id'], wrong_env['xml'])
        self.store.forget(self.project, [wanted['id']])
        self.assertNotIn(wanted['id'], build_project_brief(self.store, self.project, query=query)['xml'])
        unknown = build_project_brief(self.store, self.project, query='What requirements apply to OTHER-92?')
        self.assertNotIn(hidden['id'], unknown['xml'])


if __name__ == '__main__':
    unittest.main()
