"""Neutral custody of consumed marked user sources through public interfaces."""
import json
from pathlib import Path
import tempfile
import unittest

from codex_mem.config import configure
from codex_mem.episode_processor import process_project_memory
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store
from test_episode_processor import receipt


class IntentCustodyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / 'project'
        self.project.mkdir()
        self.data = Path(temporary.name) / 'memory'
        configure(self.data, capture_scope='all', project_memory_mode='pilot',
                  project_memory_projects=[str(self.project)], jev_quality_enabled=False)

    def remember(self, body, session='first', project=None):
        with Store(self.data) as store:
            return store.remember(project or self.project, 'Custody source', body,
                                  source='hook:UserPromptSubmit', session_id=session)

    def state(self, **options):
        with Store(self.data) as store:
            return ProjectMemory(store).snapshot(self.project, **options)

    def process(self, runner=None):
        result = process_project_memory(self.project, self.data, runner=runner or (
            lambda request: receipt({'episodes': [], 'claims': [], 'relations': []})))
        self.assertEqual(result['status'], 'processed', result)
        return result

    def test_omitted_constraint_survives_consumption_as_neutral_unresolved_source(self):
        body = 'Do not deploy CUS-17 before approval.'
        source = self.remember(body)
        self.process()
        state = self.state()
        self.assertEqual(state['coverage']['pending_events'], 0)
        self.assertEqual(state['claims'], [])
        custody = state['intent_custody']
        self.assertEqual(len(custody), 1)
        self.assertEqual(custody[0]['source_id'], source['id'])
        self.assertEqual(custody[0]['text'], body)
        self.assertEqual(custody[0]['disposition'], 'unresolved')
        self.assertFalse(custody[0]['is_requirement_claim'])
        self.assertNotIn('active_requirement', custody[0])
        self.assertEqual(custody[0]['uncertainty'], 'extracted_representation_completeness_unresolved')
        self.assertEqual(state['intent_custody_coverage']['unresolved'], 1)

    def exact_claim(self, request):
        supplied = json.loads(request['prompt'].split('\n', 1)[1])
        event = next(e for e in supplied['events'] if e['source_role'] == 'user_intent' and not e.get('historical'))
        text = ''.join(span['text'] for span in event['spans'])
        refs = [span['ref'] for span in event['spans']]
        return receipt({'episodes': [], 'claims': [{'kind': 'constraint', 'text': text,
            'rationale': '', 'workflow_state': 'open', 'scope': {'environment': '', 'version': ''},
            'evidence': {'text': refs, 'rationale': [], 'workflow_state': refs}}], 'relations': []})

    def test_full_exact_claim_is_stored_and_representation_deletion_restores_unresolved(self):
        source = self.remember('Please preserve CUS-17 deployment approval.')
        self.process(self.exact_claim)
        state = self.state()
        self.assertEqual(state['intent_custody'][0]['disposition'], 'stored')
        with Store(self.data) as store:
            ProjectMemory(store).forget(self.project, [state['claims'][0]['id']])
        state = self.state()
        self.assertEqual(state['intent_custody'][0]['disposition'], 'unresolved')
        self.assertEqual(state['intent_custody'][0]['source_id'], source['id'])

    def test_ordinary_and_quoted_sources_remain_neutral_without_active_requirements(self):
        for body in ('Keep CUS-17 logs.', 'Example text: "Do not deploy CUS-17."', 'How does CUS-17 work?'):
            self.remember(body)
        self.process()
        state = self.state()
        self.assertEqual(state['intent_custody_coverage']['unresolved'], 3)
        self.assertTrue(all(not row['is_requirement_claim'] for row in state['intent_custody']))
        self.assertEqual(state['claims'], [])
        self.assertEqual(state['claims'], [])

    def test_partial_compound_claim_cannot_discharge_uncaptured_remainder(self):
        body = 'Keep CUS-17 logs. Do not deploy CUS-17.'
        self.remember(body)
        def partial(request):
            result = self.exact_claim(request)
            supplied = json.loads(request['prompt'].split('\n', 1)[1])
            first = supplied['events'][0]['spans'][0]
            claim = result['output']['claims'][0]
            claim['text'] = first['text']
            claim['evidence']['text'] = [first['ref']]
            claim['evidence']['workflow_state'] = [first['ref']]
            return result
        self.process(partial)
        state = self.state()
        self.assertEqual(state['intent_custody'][0]['disposition'], 'unresolved')
        self.assertEqual(state['intent_custody'][0]['text'], body)

    def test_project_and_session_filters_and_bounded_omissions(self):
        first = self.remember('Keep CUS-17 logs.', session='first')
        second = self.remember('Please preserve CUS-18 approval.', session='second')
        self.process()
        self.process()
        state = self.state(exclude_session_id='first', signal_limit=1)
        self.assertEqual([row['source_id'] for row in state['intent_custody']], [second['id']])
        self.assertEqual(state['intent_custody_coverage']['total'], 1)
        bounded = self.state(signal_limit=1)
        self.assertEqual(bounded['intent_custody_coverage']['omitted'], 1)
        hidden = self.state(signal_limit=0)
        self.assertEqual(hidden['intent_custody'], [])
        self.assertEqual(hidden['intent_custody_coverage']['omitted'], 2)
        with Store(self.data) as store:
            foreign = ProjectMemory(store).snapshot(self.project / 'foreign')
        self.assertEqual(foreign['intent_custody'], [])
        self.assertNotEqual(first['id'], second['id'])

    def test_prune_protects_unresolved_but_explicit_source_forget_erases_it(self):
        source = self.remember('Please preserve CUS-17 logs.')
        self.process()
        with Store(self.data) as store:
            # Zero days makes the captured source eligible without altering internal timestamps.
            store.prune(days=0)
            self.assertTrue(store.get(self.project, [source['id']]))
            store.forget(self.project, [source['id']])
        self.assertEqual(self.state()['intent_custody_coverage']['total'], 0)

    def test_fragmented_consumption_is_idempotent_and_never_assumes_whole_requirement(self):
        body = 'Keep CUS-17 logs and do not deploy before approval. ' + ('Approval detail. ' * 25).rstrip()
        source = self.remember(body)
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            first = memory.claim(self.project, max_chars=256)
            result = memory.commit(first, {'episodes': [], 'claims': [], 'relations': []})
            self.assertEqual(result['status'], 'committed')
            self.assertEqual(memory.commit(first, {'episodes': [], 'claims': [], 'relations': []})['status'], 'duplicate')
            second = memory.claim(self.project, max_chars=1000)
            self.assertEqual(memory.commit(second, {'episodes': [], 'claims': [], 'relations': []})['status'], 'committed')
            state = memory.snapshot(self.project)
        rows = state['intent_custody']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['start'], 0)
        self.assertEqual(rows[0]['end'], rows[1]['start'])
        self.assertEqual(rows[1]['end'], len(body))
        self.assertTrue(all(row['source_id'] == source['id'] and row['disposition'] == 'unresolved' for row in rows))

    def commit_claim(self, text, session='first', kind='constraint', state='open', target=None, relation=None):
        self.remember(text, session=session)
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            snapshot = memory.claim(self.project)
            source = next(s for s in snapshot['sources'] if not s.get('historical'))
            ref = {'source_id': source['id'], 'start': 0, 'end': len(source['body']), 'quote': source['body']}
            claim = {'key': 'new', 'kind': kind, 'workflow_state': state, 'text': text,
                     'scope': {'environment': '', 'version': ''},
                     'evidence': {'text': [ref], 'workflow_state': [ref]}}
            relations = [{'from': 'new', 'to': target, 'type': relation, 'evidence': [ref]}] if target else []
            result = memory.commit(snapshot, {'episodes': [], 'claims': [claim], 'relations': relations})
            self.assertEqual(result['status'], 'committed')
            return result['object_ids'][0]

    def test_cross_chat_completion_uses_validated_transition_and_respects_excluded_session(self):
        target = self.commit_claim('CUS-17 approval')
        self.assertEqual(self.state()['intent_custody'][0]['disposition'], 'stored')
        self.commit_claim('CUS-17 approval completed.', session='second', kind='result', state='completed',
                          target=target, relation='closes')
        state = self.state()
        original = next(row for row in state['intent_custody'] if row['session_id'] == 'first')
        self.assertEqual(original['disposition'], 'completed')
        self.assertEqual(len(original['transition_ids']), 1)
        with Store(self.data) as store:
            transition = ProjectMemory(store).get(self.project, original['transition_ids'])[0]
        self.assertEqual(transition['evidence'][0]['quote'], 'CUS-17 approval completed.')
        self.assertEqual(self.state(exclude_session_id='second')['intent_custody'][0]['disposition'], 'stored')

    def test_replacement_is_uncertain_and_does_not_infer_cancellation(self):
        target = self.commit_claim('CUS-17 approval')
        self.commit_claim('CUS-17 approval replaced after cancellation.', session='second', kind='correction',
                          target=target, relation='replaces')
        state = self.state()
        self.assertEqual(state['intent_custody_coverage']['cancelled'], 0)
        original = next(row for row in state['intent_custody'] if row['session_id'] == 'first')
        self.assertEqual(original['disposition'], 'unresolved')

    def test_partial_compound_cancellation_retains_the_whole_unresolved_remainder(self):
        self.remember('CUS-17 approval. Keep CUS-17 logs.')
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            snapshot = memory.claim(self.project)
            source = snapshot['sources'][0]
            ref = {'source_id': source['id'], 'start': 0, 'end': len('CUS-17 approval.'), 'quote': 'CUS-17 approval.'}
            result = memory.commit(snapshot, {'episodes': [], 'claims': [{'key': 'first', 'kind': 'requirement',
                'text': 'CUS-17 approval.', 'workflow_state': 'open', 'scope': {'environment': '', 'version': ''},
                'evidence': {'text': [ref], 'workflow_state': [ref]}}], 'relations': []})
        self.commit_claim('CUS-17 approval replaced after cancellation.', session='second', kind='correction',
                          target=result['object_ids'][0], relation='replaces')
        original = next(row for row in self.state()['intent_custody'] if row['session_id'] == 'first')
        self.assertEqual(original['disposition'], 'unresolved')
        self.assertIn('Keep CUS-17 logs.', original['text'])

    def test_paraphrase_never_claims_full_representation(self):
        self.remember('Do not deploy CUS-17 before approval.')
        def paraphrase(request):
            result = self.exact_claim(request)
            result['output']['claims'][0]['text'] = 'CUS-17 deployment needs approval.'
            return result
        self.process(paraphrase)
        self.assertEqual(self.state()['intent_custody'][0]['disposition'], 'unresolved')

    def test_failed_commit_does_not_create_custody_or_consume_source(self):
        self.remember('Keep CUS-17 approval.')
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            snapshot = memory.claim(self.project)
            with self.assertRaises(ValueError):
                memory.commit(snapshot, {'episodes': [], 'claims': [{'bad': True}], 'relations': []})
            state = memory.snapshot(self.project)
        self.assertEqual(state['intent_custody'], [])
        self.assertEqual(state['coverage']['pending_events'], 1)

    def test_long_source_returns_bounded_excerpt_with_full_source_pointer(self):
        body = 'Keep CUS-17 evidence. ' + ('Original approval record. ' * 90).rstrip()
        self.remember(body)
        self.process()
        row = self.state()['intent_custody'][0]
        self.assertEqual(len(row['text']), 1200)
        self.assertTrue(row['truncated'])
        self.assertEqual((row['start'], row['end']), (0, len(body)))
        self.assertEqual(row['disposition'], 'unresolved')

    def test_later_exact_task_source_is_selected_before_custody_limit(self):
        from codex_mem.project_memory_retrieval import build_project_brief
        for index in range(12):
            self.remember(f'Do not deploy OLD-{index + 10} before approval.')
        requirement = 'Do not deploy CUS-999 before the signed review.'
        target = self.remember(requirement)
        self.process()
        state = self.state(query='CUS-999')
        self.assertEqual([row['source_id'] for row in state['intent_custody']], [target['id']])
        coverage = state['intent_custody_coverage']
        self.assertEqual(coverage['total'], 13)
        self.assertEqual(coverage['matched'], 1)
        self.assertEqual(coverage['unmatched'], 12)
        self.assertEqual(coverage['matched_omitted'], 0)
        with Store(self.data) as store:
            brief = build_project_brief(store, self.project, query='Continue CUS-999', budget=6000)
            context = store.context(self.project, query='Continue CUS-999', budget=6000, allow_remote=False)
        self.assertIn(requirement, brief['xml'])
        self.assertIn(requirement, context)
        self.assertLessEqual(len(brief['xml']), 6000)
        self.assertLessEqual(len(context), 6000)

    def test_query_coverage_separates_unmatched_sources_from_matching_omissions(self):
        for index in range(10):
            self.remember(f'Keep CUS-999 record {index}.')
        for index in range(2):
            self.remember(f'Keep OLD-17 unrelated record {index}.')
        self.process()
        selected = self.state(query='CUS-999', signal_limit=2)
        coverage = selected['intent_custody_coverage']
        self.assertEqual((coverage['total'], coverage['matched'], coverage['unmatched']), (12, 10, 2))
        self.assertEqual((coverage['returned'], coverage['omitted'], coverage['matched_omitted']), (2, 10, 8))
        self.assertFalse(coverage['matched_complete'])
        self.assertTrue(coverage['query_filtered'])
        natural = self.state(query='What is current project status?')
        unfiltered = self.state()
        self.assertEqual(natural['intent_custody'], unfiltered['intent_custody'])
        self.assertFalse(natural['intent_custody_coverage']['query_filtered'])

    def test_query_preselection_preserves_session_exclusion(self):
        self.remember('Keep CUS-999 private review.', session='excluded')
        self.remember('Keep OLD-17 public review.', session='visible')
        self.process()
        self.process()
        filtered = self.state(query='CUS-999', exclude_session_id='excluded')
        self.assertEqual(filtered['intent_custody'], [])
        self.assertEqual(filtered['intent_custody_coverage']['total'], 1)
        self.assertEqual(filtered['intent_custody_coverage']['matched'], 0)
