"""Bounded original-user coverage through the public Store/build/render seam."""
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree

from codex_mem.config import configure
from codex_mem.project_memory import ProjectMemory
from codex_mem.project_memory_retrieval import build_project_brief, render_project_brief
from codex_mem.store import Store
from codex_mem.tool_io import normalize_capture


class SourceBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / 'index'
        self.project.mkdir()
        self.store = Store(Path(temporary.name) / 'data')
        self.addCleanup(self.store.close)
        configure(self.store.data_dir, capture_scope='all', project_memory_mode='pilot',
                  project_memory_projects=[str(self.project)])

    def populate(self, suffix=''):
        texts = [
            'BND-72 initially requested a public archive with automatic corrections.',
            'BND-72 now needs a reviewer preview. The archivist found ambiguous names. Preserve source spellings. Do not publish before review.',
            'BND-72 removes automatic corrections. Keep the proposed-spelling table.',
            'BND-72 continues the archive preview. Finished BND-19 indexing does not approve this preview.',
            'BND-72 cannot use the removed handbook as evidence. Ask the archivist for a current source page.',
            'BND-72 reviewer Neri must resolve two names before the next review. Publication remains pending.'
        ]
        rows = [self.store.remember(self.project, 'Archive source', text + suffix,
            source='hook:UserPromptSubmit', session_id='bundle') for i, text in enumerate(texts)]
        self.store.remember(self.project, 'Earlier assistant report', 'BND-72 public archive is already live.',
            source='hook:Stop', session_id='bundle')
        output = 'BND-72 preview saved. Publication: not attempted.'
        capture = normalize_capture({'tool_name': 'functions.exec_command', 'tool_use_id': 'archive-inspect',
            'session_id': 'bundle', 'tool_input': {'cmd': 'archive inspect BND-72'},
            'tool_response': {'exit_code': 0, 'output': output}}, project=self.project)
        tool = self.store.remember(self.project, 'Archive inspection', output,
            source='hook:PostToolUse', session_id='bundle', tool_capture=capture)
        def ref(row):
            return {'source_id': row['id'], 'start': 0, 'end': len(row['body']), 'quote': row['body']}
        fields = {'summary': rows, 'goal': [rows[1], rows[2], rows[3]],
                  'rationale': [rows[1], rows[4]], 'open_work': [rows[4], rows[5]], 'outcome': [tool]}
        episode = {'key': 'archive', **{name: '\n'.join(row['body'] for row in items) for name, items in fields.items()},
                   'evidence': {name: [ref(row) for row in items] for name, items in fields.items()}}
        memory = ProjectMemory(self.store)
        result = memory.commit(memory.claim(self.project), {'episodes': [episode], 'claims': [], 'relations': []})
        self.assertEqual(result['status'], 'committed', result)
        return rows, tool, result['object_ids'][0]

    def test_complete_compound_sources_and_execution_survive_together(self):
        rows, tool, episode = self.populate()
        brief = build_project_brief(self.store, self.project, query='What output does the evidence establish?', budget=6000)
        tree = ElementTree.fromstring(brief['xml'])
        text = ''.join(tree.itertext())
        for row in rows[1:]:
            self.assertIn(row['body'], text, brief['xml'])
        self.assertIn(tool['body'], text, brief['xml'])
        self.assertIsNotNone(tree.find("./item[@id='" + episode + "'][@is_current_head='True']"))
        bundle = tree.find("./item[@lane='intent_custody'][@source_bundle='True']")
        self.assertIsNotNone(bundle, brief['xml'])
        self.assertEqual(bundle.get('is_requirement_claim'), 'False')
        pointers = bundle.findall('./field/source')
        seqs = [int(pointer.get('source_capture_seq')) for pointer in pointers]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertTrue(all(pointer.get('source_role') == 'user_intent' for pointer in pointers))
        self.assertLessEqual(len(brief['xml']), 6000)

    def test_source_bundle_keeps_scope_session_and_deletion_fences(self):
        rows, _, _ = self.populate()
        foreign = self.project.parent / 'foreign'
        foreign.mkdir()
        hidden = self.store.remember(foreign, 'Foreign', 'BND-72 foreign-only rule.',
            source='hook:UserPromptSubmit', session_id='foreign')
        query = 'What output does the evidence establish?'
        excluded = build_project_brief(self.store, self.project, query=query, exclude_session_id='bundle')
        self.assertNotIn(rows[2]['body'], excluded['xml'])
        self.assertNotIn(hidden['body'], excluded['xml'])
        self.store.forget(self.project, [rows[4]['id']])
        deleted = build_project_brief(self.store, self.project, query=query)
        self.assertNotIn(rows[4]['body'], deleted['xml'])
        unknown = build_project_brief(self.store, self.project, query='What happened to OTHER-99?')
        self.assertEqual(unknown['records'], [])

    def test_re_render_small_budget_keeps_whole_latest_originals_and_counts(self):
        rows, _, _ = self.populate()
        brief = build_project_brief(self.store, self.project, query='What output does the evidence establish?', budget=6000)
        small = render_project_brief(brief, budget=1800)
        tree = ElementTree.fromstring(small)
        for pointer in tree.findall("./item[@lane='intent_custody']/field/source"):
            row = next(row for row in rows if row['id'] == pointer.get('source_id'))
            self.assertEqual(pointer.findtext('quote_excerpt'), row['body'])
            self.assertEqual(pointer.get('quote_truncated'), 'False')
        self.assertIsNotNone(tree.find('omitted'))
        self.assertLessEqual(len(small), 1800)

    def test_original_of_selected_source_keeps_adjacent_clause_without_soft_topic_match(self):
        topic = self.store.remember(self.project, 'Paper gate', 'Paper gate investigation.',
            source='hook:UserPromptSubmit', session_id='gate')
        rule = self.store.remember(self.project, 'Evidence rule',
            'The retired ledger cannot establish approval. Ask Neri to inspect the original.',
            source='hook:UserPromptSubmit', session_id='gate')
        def ref(row, start=0):
            return {'source_id': row['id'], 'start': start, 'end': len(row['body']), 'quote': row['body'][start:]}
        memory = ProjectMemory(self.store)
        episode = {'key': 'gate', 'summary': topic['body'], 'goal': topic['body'],
            'open_work': 'Ask Neri to inspect the original.', 'evidence': {
                'summary': [ref(topic)], 'goal': [ref(topic)],
                'open_work': [ref(rule, rule['body'].index('Ask'))]}}
        self.assertEqual(memory.commit(memory.claim(self.project),
            {'episodes': [episode], 'claims': [], 'relations': []})['status'], 'committed')
        brief = build_project_brief(self.store, self.project, query='What should happen before the paper gate?')
        self.assertIn(rule['body'], ''.join(ElementTree.fromstring(brief['xml']).itertext()))

    def test_bundle_obeys_exact_versions_environments_and_private_queries(self):
        rows, _, _ = self.populate(' Version 2.8.4 in staging.')
        near = self.store.remember(self.project, 'Different scope', 'BND-72 version 2.8.40 production-only instruction.',
            source='hook:UserPromptSubmit', session_id='near')
        memory = ProjectMemory(self.store)
        self.assertEqual(memory.commit(memory.claim(self.project),
            {'episodes': [], 'claims': [], 'relations': []})['status'], 'committed')
        brief = build_project_brief(self.store, self.project,
            query='What happened to BND-72 version 2.8.4 in staging?')
        tree = ElementTree.fromstring(brief['xml'])
        self.assertTrue(tree.findall("./item[@source_bundle='True']/field/source"), brief['xml'])
        self.assertNotIn(near['body'], brief['xml'])
        self.assertTrue(all('2.8.4' in pointer.findtext('quote_excerpt') for pointer in
            tree.findall("./item[@source_bundle='True']/field/source")))
        private = build_project_brief(self.store, self.project, query='<private>BND-72</private>')
        self.assertEqual(private['xml'], '')

    def test_compressed_episode_associations_are_explicitly_omitted(self):
        self.populate()
        brief = build_project_brief(self.store, self.project, query='What output does the evidence establish?')
        tree = ElementTree.fromstring(brief['xml'])
        compressed = tree.findall("./item[@field_associations_omitted]")
        self.assertTrue(compressed, brief['xml'])
        for item in compressed:
            self.assertGreaterEqual(int(item.get('fields_omitted')), int(item.get('field_associations_omitted')))
            self.assertNotIn('source_groups_quoted_above', item.attrib)

    def test_bundle_exact_fit_and_one_character_below_keep_complete_sources(self):
        rows, _, _ = self.populate()
        brief = build_project_brief(self.store, self.project, query='What output does the evidence establish?')
        bundle = next(record for record in brief['records'] if record.get('source_bundle'))
        original = {'records': [bundle], 'coverage': {}}
        full = render_project_brief(original, budget=6000)
        exact = render_project_brief(original, budget=len(full))
        self.assertEqual(exact, full)
        below = render_project_brief(original, budget=len(full) - 1)
        tree = ElementTree.fromstring(below)
        pointers = tree.findall('./item/field/source')
        self.assertEqual(len(pointers), len(rows) - 1)
        self.assertNotIn(rows[0]['id'], [pointer.get('source_id') for pointer in pointers])
        self.assertEqual(tree.find('item').get('fields_omitted'), '1')
        self.assertEqual(tree.find('omitted').get('user_sources'), '1')
        for pointer in pointers:
            row = next(row for row in rows if row['id'] == pointer.get('source_id'))
            self.assertEqual(pointer.findtext('quote_excerpt'), row['body'])
        self.assertLessEqual(len(below), len(full) - 1)

    def test_scoped_claim_build_respects_minimum_budget(self):
        source = self.store.remember(self.project, 'Approval', 'BND-72 needs approval.',
            source='hook:UserPromptSubmit', session_id='approval')
        ref = {'source_id': source['id'], 'start': 0, 'end': len(source['body']), 'quote': source['body']}
        memory = ProjectMemory(self.store)
        claim = {'key': 'approval', 'kind': 'requirement', 'text': source['body'], 'workflow_state': 'open',
            'scope': {'environment': '', 'version': ''}, 'evidence': {'text': [ref], 'workflow_state': [ref]}}
        self.assertEqual(memory.commit(memory.claim(self.project),
            {'episodes': [], 'claims': [claim], 'relations': []})['status'], 'committed')
        brief = build_project_brief(self.store, self.project, query='What happened to BND-72?', budget=128)
        self.assertLessEqual(len(brief['xml']), 128)
        tree = ElementTree.fromstring(brief['xml'])
        self.assertIsNone(tree.get('worktree'))
        self.assertEqual(brief['claims'][0]['scope']['worktree'], str(self.project.resolve()))

    def test_minimum_renderer_budget_does_not_promote_oversized_root(self):
        project = '/private/tmp/example-project'
        record = {'id': 'c', 'lane': 'claim', 'scope': {'worktree': project}, 'fields': []}
        xml = render_project_brief({'project': project, 'records': [record]}, budget=128)
        self.assertLessEqual(len(xml), 128)
        self.assertIsNone(ElementTree.fromstring(xml).get('worktree'))
        self.assertEqual(record['scope']['worktree'], project)

    def test_unpromoted_long_root_keeps_retained_record_scope_and_capture_basis(self):
        project = '/private/tmp/' + 'long-directory-' * 100
        other = '/private/tmp/other-project'
        text = 'Keep approval.'
        retained = {'id': 'short', 'lane': 'claim', 'scope': {'worktree': other},
            'capture_order_basis': 'local_capture_sequence_not_event_time',
            'fields': [{'name': 'text', 'text': text, 'evidence': [{'source_id': 'source',
                'start': 0, 'end': len(text), 'source_role': 'user_intent', 'quote_excerpt': text}]}]}
        records = [{'id': 'long', 'lane': 'claim', 'scope': {'worktree': project}, 'fields': []}, retained]
        xml = render_project_brief({'project': project, 'records': records}, budget=700)
        tree = ElementTree.fromstring(xml)
        self.assertLessEqual(len(xml), 700)
        self.assertIsNone(tree.get('worktree'))
        item = tree.find("./item[@id='short']")
        self.assertIsNotNone(item, xml)
        self.assertEqual(item.get('worktree'), other)
        self.assertEqual(item.get('capture_order_basis'), 'local_capture_sequence_not_event_time')
        self.assertEqual(retained['scope']['worktree'], other)


if __name__ == '__main__':
    unittest.main()
