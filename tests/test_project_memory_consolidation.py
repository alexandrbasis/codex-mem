"""Consolidation through public processing and temporary durable storage."""
import json
from pathlib import Path
import tempfile
import unittest

from codex_mem.config import configure
from codex_mem.episode_processor import process_project_memory
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store
from test_episode_processor import receipt


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        self.data = Path(self.temp.name) / 'memory'
        configure(self.data, capture_scope='all', project_memory_mode='shadow',
                  project_memory_projects=[str(self.project)], jev_quality_enabled=False)

    def state(self):
        with Store(self.data) as store:
            return ProjectMemory(store).snapshot(self.project)

    def remember(self, body, session='one', project=None, source='hook:UserPromptSubmit'):
        with Store(self.data) as store:
            return store.remember(project or self.project, 'Task', body, source=source, session_id=session)

    def process(self, runner):
        return process_project_memory(self.project, self.data, runner=runner)

    def standalone(self, request):
        supplied = json.loads(request['prompt'].split('\n', 1)[1])
        event = supplied['events'][0]
        spans = event['spans']
        item = {'summary': ''.join(s['text'] for s in spans), 'goal': 'Task JOB-17', 'rationale': '', 'outcome': '', 'open_work': '',
                'continuation_of': None,
                'evidence': {'summary': [s['ref'] for s in spans], 'goal': [spans[0]['ref']],
                             'rationale': [], 'outcome': [], 'open_work': [], 'continuation_of': []}}
        return receipt({'episodes': [item], 'claims': [], 'relations': []})

    def split(self):
        first = self.remember('Task JOB-17: build the importer. Keep deployment pending.', 'one')
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        second = self.remember('Continue task JOB-17. Local tests passed; production is unchecked.', 'two')
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        self.assertEqual(len(self.state()['episodes']), 2)
        return first, second

    def consolidate(self, request):
        supplied = json.loads(request['prompt'].split('\n', 1)[1])
        self.assertEqual(supplied['job_kind'], 'reconsideration')
        heads = supplied['current_state']['episodes']
        spans = [s for e in supplied['events'] for s in e['spans']]
        refs = [s['ref'] for s in spans]
        item = {'summary': 'JOB-17 importer: local tests passed; production is unchecked.',
                'goal': 'Task JOB-17', 'rationale': '', 'outcome': 'Local tests passed; production is unchecked.',
                'open_work': 'Keep deployment pending.', 'continuation_of': heads[0]['id'],
                'consolidation_of': [e['id'] for e in heads[1:]],
                'evidence': {'summary': refs, 'goal': refs[:1], 'rationale': [], 'outcome': refs[-1:],
                             'open_work': refs[1:2], 'continuation_of': refs,
                             'consolidation_of': refs}}
        return receipt({'episodes': [item], 'claims': [], 'relations': []})

    def test_split_task_is_reconsidered_after_restart_with_original_field_evidence(self):
        first, second = self.split()
        before = self.state()['episodes']
        result = self.process(self.consolidate)
        self.assertEqual(result['status'], 'processed', result)
        current = self.state()['episodes']
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['open_work'], 'User intent: Keep deployment pending.')
        self.assertEqual({r['source_id'] for refs in current[0]['evidence'].values() for r in refs},
                         {first['id'], second['id']})
        with Store(self.data) as store:
            self.assertTrue(all(not item['is_current_head'] for item in
                                ProjectMemory(store).get(self.project, [item['id'] for item in before])))
        self.assertEqual(self.process(lambda request: self.fail('replayed completed consolidation'))['status'], 'idle')

    def test_unresolved_reconsideration_is_visible_and_not_retried_forever(self):
        self.split()
        self.assertTrue(self.state()['coverage']['pending_reconciliation'])
        result = self.process(lambda request: receipt())
        self.assertEqual(result['status'], 'processed', result)
        self.assertTrue(result['pending_reconciliation'])
        self.assertEqual(len(self.state()['episodes']), 2)
        self.assertTrue(self.state()['coverage']['pending_reconciliation'])
        self.assertTrue(self.state()['coverage']['reconsideration']['candidate_window_bounded'])
        self.assertEqual(self.process(lambda request: self.fail('repeated no-op'))['status'], 'idle')

    def test_consolidation_cannot_drop_a_retained_open_obligation(self):
        self.remember('Task JOB-17: build the importer. Keep deployment pending.', 'one')
        def first(request):
            result = self.standalone(request)
            event = json.loads(request['prompt'].split('\n', 1)[1])['events'][0]
            result['output']['episodes'][0]['open_work'] = 'Keep deployment pending.'
            result['output']['episodes'][0]['evidence']['open_work'] = [event['spans'][1]['ref']]
            return result
        self.assertEqual(self.process(first)['status'], 'processed')
        self.remember('Continue task JOB-17. Local tests passed; production is unchecked.', 'two')
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        def omit(request):
            result = self.consolidate(request)
            result['output']['episodes'][0]['open_work'] = ''
            result['output']['episodes'][0]['evidence']['open_work'] = []
            return result
        result = self.process(omit)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(len(self.state()['episodes']), 2)
        self.assertIn('User intent: Keep deployment pending.', [item.get('open_work') for item in self.state()['episodes']])

    def test_late_correction_without_stop_can_retain_original_goal_and_obligation(self):
        first, _ = self.split()
        self.assertEqual(self.process(self.consolidate)['status'], 'processed')
        correction = self.remember('Correction for task JOB-17. Local tests actually failed; production is unchecked.', 'three')
        def correct(request):
            supplied = json.loads(request['prompt'].split('\n', 1)[1])
            originals = {event['id']: event for event in supplied['events']}
            self.assertIn(first['id'], originals)
            head = supplied['current_state']['episodes'][0]
            old = originals[first['id']]['spans']
            fresh = originals[correction['id']]['spans']
            item = {'summary': 'JOB-17 local tests failed; deployment remains pending.', 'goal': 'Task JOB-17',
                    'rationale': '', 'outcome': 'Local tests actually failed; production is unchecked.',
                    'open_work': 'Keep deployment pending.', 'continuation_of': head['id'],
                    'evidence': {'summary': [fresh[1]['ref'], old[1]['ref']], 'goal': [old[0]['ref']],
                                 'rationale': [], 'outcome': [fresh[1]['ref']], 'open_work': [old[1]['ref']],
                                 'continuation_of': [fresh[0]['ref']]}}
            return receipt({'episodes': [item], 'claims': [], 'relations': []})
        result = self.process(correct)
        self.assertEqual(result['status'], 'processed', result)
        head = self.state()['episodes'][0]
        self.assertEqual(head['outcome'], 'User intent: Local tests actually failed; production is unchecked.')
        self.assertEqual(head['evidence']['goal'][0]['source_id'], first['id'])
        self.assertEqual(self.state()['coverage']['pending_events'], 0)

    def test_reconsideration_cannot_loop_by_only_revising_one_split_head(self):
        self.split()
        def no_merge(request):
            result = self.consolidate(request)
            result['output']['episodes'][0]['consolidation_of'] = []
            result['output']['episodes'][0]['evidence']['consolidation_of'] = []
            return result
        result = self.process(no_merge)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(self.process(lambda request: self.fail('repeated invalid repair'))['status'], 'idle')
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_independent_tasks_in_same_chat_are_not_joined_by_generated_title(self):
        self.remember('Task JOB-17: build the importer.', 'same-chat')
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        self.remember('Task JOB-99: build the exporter.', 'same-chat')
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        self.assertEqual(self.process(lambda request: self.fail('different original task identities'))['status'], 'idle')
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_negated_identity_and_assistant_reports_cannot_authorize_a_merge(self):
        self.remember('Task JOB-17: build the importer.', 'one')
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        self.remember('This is not a continuation of task JOB-17.', 'two')
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        self.remember('Continue task JOB-17. Production deployment is complete.', 'three',
                      source='hook:Stop')
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        self.assertEqual(self.process(lambda request: self.fail('unproved identity'))['status'], 'idle')
        self.assertEqual(len(self.state()['episodes']), 3)
        self.assertTrue(self.state()['coverage']['pending_reconciliation'])

    def test_two_projects_do_not_disclose_sources_or_current_heads(self):
        self.split()
        foreign = Path(self.temp.name) / 'foreign'
        foreign.mkdir()
        configure(self.data, project_memory_projects=[str(self.project), str(foreign)])
        foreign_source = self.remember('Task JOB-17: private foreign requirement.', 'foreign', project=foreign)
        self.assertEqual(process_project_memory(foreign, self.data, runner=self.standalone)['status'], 'processed')
        def run(request):
            self.assertNotIn(foreign_source['id'], request['prompt'])
            self.assertNotIn('private foreign requirement', request['prompt'])
            return self.consolidate(request)
        self.assertEqual(self.process(run)['status'], 'processed')
        with Store(self.data) as store:
            self.assertEqual(len(ProjectMemory(store).snapshot(foreign)['episodes']), 1)

    def test_deletion_during_reconsideration_fences_old_result_and_keeps_accounting(self):
        first, _ = self.split()
        def run(request):
            result = self.consolidate(request)
            with Store(self.data) as store:
                store.forget(self.project, [first['id']])
            return result
        result = self.process(run)
        self.assertEqual(result['status'], 'deferred', result)
        self.assertEqual(result['code'], 'source_changed')
        self.assertEqual(len(self.state()['episodes']), 1)
        self.assertEqual(self.process(lambda request: self.fail('restored deleted task'))['status'], 'idle')
        with Store(self.data) as store:
            attempts = ProjectMemory(store).attempts(self.project)
            self.assertEqual(len(attempts), 3)
            self.assertTrue(all(attempt['outcome'] != 'running' for attempt in attempts))

    def test_forgetting_an_absorbed_episode_revokes_consolidated_descendants(self):
        self.split()
        old = self.state()['episodes']
        self.assertEqual(self.process(self.consolidate)['status'], 'processed')
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            memory.forget(self.project, [old[0]['id']])
        self.assertEqual(self.state()['episodes'], [])
        self.assertEqual(self.process(lambda request: self.fail('resurrected forgotten head'))['status'], 'idle')

    def test_concurrent_committed_event_rejects_reconsideration_then_retries_current_state(self):
        self.split()
        def run(request):
            result = self.consolidate(request)
            self.remember('An unrelated observation.', 'concurrent')
            self.assertEqual(self.process(lambda request: receipt())['status'], 'processed')
            return result
        result = self.process(run)
        self.assertEqual(result['status'], 'deferred', result)
        self.assertEqual(len(self.state()['episodes']), 2)
        self.assertEqual(self.process(self.consolidate)['status'], 'processed')
        self.assertEqual(len(self.state()['episodes']), 1)

    def test_failed_attempt_can_retry_after_restart_without_overwriting_costs(self):
        self.split()
        def fail(request):
            raise TimeoutError('synthetic boundary failure')
        result = self.process(fail)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(self.process(lambda request: self.fail('ignored backoff'))['status'], 'idle')
        with Store(self.data) as store:
            self.assertEqual(ProjectMemory(store).retry_failed(self.project), 1)
        result = self.process(self.consolidate)
        self.assertEqual(result['status'], 'processed', result)
        with Store(self.data) as store:
            attempts = ProjectMemory(store).attempts(self.project)
            self.assertEqual(len(attempts), 4)
            self.assertEqual(sum(item['outcome'] == 'failed' for item in attempts), 1)
            self.assertEqual(sum(item['outcome'] == 'processed' for item in attempts), 3)

    def test_identity_discovery_budget_is_visible_and_does_not_guess_from_title(self):
        def small(request):
            result = self.standalone(request)
            spans = json.loads(request['prompt'].split('\n', 1)[1])['events'][0]['spans']
            result['output']['episodes'][0]['summary'] = 'Task JOB-17: build importer.'
            result['output']['episodes'][0]['evidence']['summary'] = [spans[0]['ref']]
            return result
        for session in ('one', 'two'):
            self.remember('Task JOB-17: build importer. ' + 'x' * 13000, session)
            self.assertEqual(self.process(small)['status'], 'processed')
        self.assertEqual(self.process(lambda request: self.fail('identity disclosure exceeds budget'))['status'], 'idle')
        coverage = self.state()['coverage']['reconsideration']
        self.assertGreater(coverage['identity_sources_omitted'], 0)
        self.assertEqual(coverage['identity_char_limit'], 24000)
        self.assertTrue(self.state()['coverage']['pending_reconciliation'])

    def test_optional_quality_gate_checks_consolidation_against_its_own_spans(self):
        from codex_mem.jev_quality import MODEL
        self.split()
        configure(self.data, jev_quality_enabled=True)
        checked = []
        def evaluate(payload):
            answers = {}
            for item in payload['state']['items']:
                supported = item['candidate']['title'] != 'Project memory consolidation'
                if not supported:
                    checked.append(item)
                for suffix in ('grounded', 'overclaim'):
                    answers[f"item_{item['item_index']}_{suffix}"] = {
                        'type': 'noul', 'noul': .99 if supported == (suffix == 'grounded') else .01}
            return {'model': MODEL, 'answers': answers, 'usage': {'input_tokens': 7, 'output_tokens': 3}}
        result = process_project_memory(self.project, self.data, runner=self.consolidate,
                                        jev_quality_evaluator=evaluate)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(len(self.state()['episodes']), 2)
        self.assertEqual(len(checked), 1)
        self.assertEqual(len(checked[0]['reference_only_record']['prior_episodes']), 1)
        self.assertTrue(checked[0]['field_evidence'])

    def test_partial_narrative_cannot_erase_an_earlier_open_obligation(self):
        source = self.remember('Task JOB-17: build the importer. Keep deployment pending. Preserve manual approval.', 'one')
        def initial(request):
            result = self.standalone(request)
            spans = json.loads(request['prompt'].split('\n', 1)[1])['events'][0]['spans']
            item = result['output']['episodes'][0]
            item['open_work'] = 'Keep deployment pending. Preserve manual approval.'
            item['evidence']['open_work'] = [span['ref'] for span in spans[1:]]
            return result
        self.assertEqual(self.process(initial)['status'], 'processed')
        self.remember('Continue task JOB-17. Local tests passed; production is unchecked.', 'two')
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        self.assertEqual(self.process(self.consolidate)['status'], 'processed')
        state = self.state()
        episode = state['episodes'][0]
        self.assertEqual(episode['prior_open_work'], 'User intent: Keep deployment pending.\nUser intent: Preserve manual approval.')
        self.assertEqual(episode['prior_context_status'], 'historical_current_validity_unresolved')
        self.assertEqual({ref['source_id'] for ref in episode['evidence']['prior_open_work']}, {source['id']})
        self.assertIn(' Preserve manual approval.', [ref['quote'] for ref in episode['evidence']['prior_open_work']])
        self.assertEqual(state['claims'], [])
        self.assertTrue(state['coverage']['pending_reconciliation'])

    def test_new_head_reopens_bounded_reconsideration_after_an_older_noop(self):
        for number in range(4):
            self.remember(f'Continue task JOB-17. Checkpoint {number}.', f'session-{number}')
            self.assertEqual(self.process(self.standalone)['status'], 'processed')
        self.assertEqual(self.process(lambda request: receipt())['status'], 'processed')
        latest = self.remember('Correction for task JOB-17. Checkpoint newest.', 'latest')
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        observed = []
        def run(request):
            supplied = json.loads(request['prompt'].split('\n', 1)[1])
            observed.extend(event['id'] for event in supplied['events'])
            return receipt()
        self.assertEqual(self.process(run)['status'], 'processed')
        self.assertIn(latest['id'], observed)
        self.assertEqual(self.state()['coverage']['reconsideration']['omitted_heads'], 1)

    def test_explicitly_closed_existing_requirement_is_not_reopened_by_carry(self):
        self.remember('Task JOB-17: build the importer. Keep deployment open.', 'one')
        def initial(request):
            result = self.standalone(request)
            span = json.loads(request['prompt'].split('\n', 1)[1])['events'][0]['spans'][1]['ref']
            episode = result['output']['episodes'][0]
            episode['open_work'] = 'Keep deployment open.'
            episode['evidence']['open_work'] = [span]
            result['output']['claims'] = [{'text': 'Keep deployment open.', 'kind': 'requirement',
                'workflow_state': 'open', 'rationale': '', 'scope': {'environment': '', 'version': '', 'worktree': ''},
                'evidence': {'text': [span], 'workflow_state': [span], 'rationale': []}}]
            return result
        self.assertEqual(self.process(initial)['status'], 'processed')
        self.remember('Continue task JOB-17. Keep deployment open completed.', 'two')
        def close(request):
            supplied = json.loads(request['prompt'].split('\n', 1)[1])
            result = self.standalone(request)
            spans = supplied['events'][0]['spans']
            episode = result['output']['episodes'][0]
            episode['continuation_of'] = supplied['current_state']['episodes'][0]['id']
            episode['evidence']['continuation_of'] = [spans[0]['ref']]
            result['output']['claims'] = [{'text': 'Keep deployment open completed.', 'kind': 'task',
                'workflow_state': 'completed', 'rationale': '', 'scope': {'environment': '', 'version': '', 'worktree': ''},
                'evidence': {'text': [spans[1]['ref']], 'workflow_state': [spans[1]['ref']], 'rationale': []}}]
            result['output']['relations'] = [{'type': 'closes', 'from_claim_index': 0,
                'to_claim_id': supplied['current_state']['claims'][0]['id'], 'evidence': [spans[1]['ref']]}]
            return result
        result = self.process(close)
        self.assertEqual(result['status'], 'processed', result)
        requirements = [item for item in self.state()['claims'] if item['kind'] == 'requirement']
        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0]['workflow_state'], 'completed')

    def test_prior_reason_and_safety_constraint_remain_explicit_history_in_recall(self):
        self.remember('Task JOB-17: build the importer. Use streaming because exports exceed RAM. Keep a dry-run because deletion is irreversible.', 'one')
        def initial(request):
            result = self.standalone(request)
            spans = json.loads(request['prompt'].split('\n', 1)[1])['events'][0]['spans']
            item = result['output']['episodes'][0]
            item['rationale'] = 'Use streaming because exports exceed RAM. Keep a dry-run because deletion is irreversible.'
            item['evidence']['rationale'] = [span['ref'] for span in spans[1:]]
            return result
        self.assertEqual(self.process(initial)['status'], 'processed')
        self.remember('Continue task JOB-17. Local tests passed; production is unchecked.', 'two')
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        def shorten(request):
            result = self.consolidate(request)
            spans = json.loads(request['prompt'].split('\n', 1)[1])['events'][0]['spans']
            item = result['output']['episodes'][0]
            item['rationale'] = 'Use streaming because exports exceed RAM.'
            item['evidence']['rationale'] = [spans[1]['ref']]
            item['open_work'] = ''
            item['evidence']['open_work'] = []
            return result
        self.assertEqual(self.process(shorten)['status'], 'processed')
        current = self.state()['episodes'][0]
        self.assertIn('Keep a dry-run because deletion is irreversible.', current['prior_rationale'])
        self.assertEqual(current['prior_context_status'], 'historical_current_validity_unresolved')
        self.assertEqual(self.state()['claims'], [])
        from codex_mem.project_memory_retrieval import build_project_brief
        with Store(self.data) as store:
            brief = build_project_brief(store, self.project, query='Why JOB-17?', budget=6000, include_shadow=True)
        self.assertIn('deletion is irreversible', brief['xml'])
        self.assertIn('prior_rationale_source_excerpt', brief['xml'])

    def test_closed_atomic_requirement_is_not_reopened_from_old_compound_context(self):
        self.remember('Task JOB-17: build the importer. Keep deployment open. Preserve manual approval.', 'one')
        def initial(request):
            result = self.standalone(request)
            spans = json.loads(request['prompt'].split('\n', 1)[1])['events'][0]['spans']
            episode = result['output']['episodes'][0]
            episode['open_work'] = 'Keep deployment open. Preserve manual approval.'
            episode['evidence']['open_work'] = [span['ref'] for span in spans[1:]]
            result['output']['claims'] = [{'text': 'Preserve manual approval.', 'kind': 'requirement',
                'workflow_state': 'open', 'rationale': '', 'scope': {'environment': '', 'version': '', 'worktree': ''},
                'evidence': {'text': [spans[2]['ref']], 'workflow_state': [spans[2]['ref']], 'rationale': []}}]
            return result
        self.assertEqual(self.process(initial)['status'], 'processed')
        self.remember('Continue task JOB-17. Preserve manual approval completed.', 'two')
        def close(request):
            supplied = json.loads(request['prompt'].split('\n', 1)[1])
            result = self.standalone(request)
            spans = supplied['events'][0]['spans']
            episode = result['output']['episodes'][0]
            episode['continuation_of'] = supplied['current_state']['episodes'][0]['id']
            episode['evidence']['continuation_of'] = [spans[0]['ref']]
            result['output']['claims'] = [{'text': 'Preserve manual approval completed.', 'kind': 'task',
                'workflow_state': 'completed', 'rationale': '', 'scope': {'environment': '', 'version': '', 'worktree': ''},
                'evidence': {'text': [spans[1]['ref']], 'workflow_state': [spans[1]['ref']], 'rationale': []}}]
            result['output']['relations'] = [{'type': 'closes', 'from_claim_index': 0,
                'to_claim_id': supplied['current_state']['claims'][0]['id'], 'evidence': [spans[1]['ref']]}]
            return result
        self.assertEqual(self.process(close)['status'], 'processed')
        requirements = [item for item in self.state()['claims'] if item['kind'] == 'requirement']
        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0]['workflow_state'], 'completed')
        current = self.state()['episodes'][0]
        self.assertIn('Preserve manual approval.', current['prior_open_work'])
        self.assertEqual(current['prior_context_status'], 'historical_current_validity_unresolved')

    def test_retained_field_keeps_all_bounded_source_pointers_for_rendering(self):
        for index in range(2):
            self.remember('Continue task JOB-17. ' + ' '.join(f'Reason {index}-{n}.' for n in range(5)), str(index))
            def initial(request):
                result = self.standalone(request)
                spans = json.loads(request['prompt'].split('\n', 1)[1])['events'][0]['spans']
                item = result['output']['episodes'][0]
                item['rationale'] = ' '.join(span['text'].strip() for span in spans[1:])
                item['evidence']['rationale'] = [span['ref'] for span in spans[1:]]
                return result
            self.assertEqual(self.process(initial)['status'], 'processed')
        def combine(request):
            supplied = json.loads(request['prompt'].split('\n', 1)[1])
            heads = supplied['current_state']['episodes']
            refs = [event['spans'][0]['ref'] for event in supplied['events']]
            reason = supplied['events'][0]['spans'][1]
            item = {'summary': 'Task JOB-17', 'goal': 'Task JOB-17', 'rationale': reason['text'].strip(),
                'outcome': '', 'open_work': '', 'continuation_of': heads[0]['id'],
                'consolidation_of': [heads[1]['id']], 'evidence': {'summary': refs, 'goal': refs[:1],
                'rationale': [reason['ref']], 'outcome': [], 'open_work': [], 'continuation_of': refs,
                'consolidation_of': refs}}
            return receipt({'episodes': [item], 'claims': [], 'relations': []})
        self.assertEqual(self.process(combine)['status'], 'processed')
        from codex_mem.project_memory_retrieval import build_project_brief
        with Store(self.data) as store:
            brief = build_project_brief(store, self.project, include_shadow=True)
        field = next(item for item in brief['episodes'][0]['fields'] if item['name'] == 'prior_rationale')
        self.assertEqual(len(field['evidence']), 10)

    def test_native_reconsideration_contract_requires_primary_and_distinct_additional_head(self):
        self.split()
        def run(request):
            schema = request['output_schema']['properties']['episodes']['items']
            branches = schema.get('anyOf', [schema])
            for branch in branches:
                primary = branch['properties']['continuation_of']
                additional = branch['properties']['consolidation_of']
                self.assertEqual(primary['type'], 'string')
                self.assertEqual(len(primary['enum']), 1)
                self.assertEqual(additional['minItems'], 1)
                self.assertNotIn(primary['enum'][0], additional['items']['enum'])
                self.assertEqual(branch['properties']['evidence']['properties']['consolidation_of']['minItems'], 1)
            self.assertIn('In ordinary event jobs, continuation_of', request['prompt'])
            self.assertIn('original historical observations can support both continuation_of and consolidation_of', request['prompt'])
            return self.consolidate(request)
        result = self.process(run)
        self.assertEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 1)

    def named_identity_reconsideration(self, task_id, link):
        def standalone(request):
            response = self.standalone(request)
            response['output']['episodes'][0]['goal'] = 'Task ' + task_id
            return response
        self.remember(f'Task {task_id}: build the exporter. Keep deployment pending.', 'one')
        self.assertEqual(self.process(standalone)['status'], 'processed')
        self.remember(link + ' Local tests passed; production is unchecked.', 'two')
        self.assertEqual(self.process(standalone)['status'], 'processed')
        def consolidate(request):
            response = self.consolidate(request)
            episode = response['output']['episodes'][0]
            episode['goal'] = 'Task ' + task_id
            episode['summary'] = task_id + ' exporter: local tests passed; production is unchecked.'
            return response
        return self.process(consolidate)

    def test_same_named_ticket_explicitly_continues_original_identity(self):
        result = self.named_identity_reconsideration('RIV-42', 'For the same RIV-42 ticket, revise the exporter.')
        self.assertEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 1)

    def test_same_named_issue_explicitly_continues_another_identity(self):
        result = self.named_identity_reconsideration('MAP-91', 'The same MAP-91 issue needs a revised exporter.')
        self.assertEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 1)

    def test_same_component_is_not_named_task_continuity(self):
        result = self.named_identity_reconsideration('RIV-42', 'Task RIV-42 uses the same component as RIV-42.')
        self.assertEqual(result['code'], 'invalid_response', result)
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_separate_task_does_not_continue_same_named_ticket(self):
        result = self.named_identity_reconsideration('RIV-42', 'A separate task uses the same RIV-42 ticket label.')
        self.assertNotEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_same_named_task_explicitly_continues_original_identity(self):
        result = self.named_identity_reconsideration('QNX-67', 'The same QNX-67 task needs a revised exporter.')
        self.assertEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 1)

    def test_negated_same_named_ticket_does_not_establish_continuity(self):
        result = self.named_identity_reconsideration('RIV-42', 'This is not the same RIV-42 ticket.')
        self.assertNotEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_separate_issue_with_positive_identity_prefix_stays_separate(self):
        result = self.named_identity_reconsideration(
            'RIV-42', 'Task RIV-42: another exporter. A separate issue uses the same RIV-42 ticket label.')
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['code'], 'invalid_response', result)
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_separate_ticket_with_positive_identity_prefix_stays_separate(self):
        result = self.named_identity_reconsideration(
            'MAP-91', 'Task MAP-91: another exporter. A separate ticket concerns the same MAP-91 issue.')
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['code'], 'invalid_response', result)
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_same_named_ticket_label_does_not_establish_continuity(self):
        result = self.named_identity_reconsideration(
            'RIV-42', 'Task RIV-42: another exporter. The same RIV-42 ticket label appears in the export.')
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['code'], 'invalid_response', result)
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_same_named_issue_reference_does_not_establish_continuity(self):
        result = self.named_identity_reconsideration(
            'MAP-91', 'Task MAP-91: another exporter. A reference to the same MAP-91 issue appears in the export.')
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['code'], 'invalid_response', result)
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_explicit_reopening_names_the_same_ticket_before_identity_phrase(self):
        result = self.named_identity_reconsideration('LUX-54', 'Reopen LUX-54 as the same ticket.')
        self.assertEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 1)

    def test_material_episode_fields_are_attributed_to_their_own_sources(self):
        requested = self.remember('Task LUX-54: replace the carousel. Keep production deployment pending.')
        reported = self.remember('The list markup is in place locally. Production has not been contacted.', source='hook:Stop')
        def unsupported(request):
            supplied = json.loads(request['prompt'].split('\n', 1)[1])
            events = {event['id']: event for event in supplied['events']}
            user_refs = [span['ref'] for span in events[requested['id']]['spans']]
            report_refs = [span['ref'] for span in events[reported['id']]['spans']]
            item = {'summary': 'The carousel was replaced and deployed to production.',
                    'goal': 'Production rollout completed.', 'rationale': '',
                    'outcome': 'The list is deployed to production.', 'open_work': 'Nothing remains.',
                    'evidence': {'summary': user_refs + report_refs, 'goal': user_refs[:1],
                                 'rationale': [], 'outcome': report_refs, 'open_work': user_refs[1:]}}
            return receipt({'episodes': [item], 'claims': [], 'relations': []})
        self.assertEqual(self.process(unsupported)['status'], 'processed')
        current = self.state()['episodes'][0]
        self.assertEqual(current['outcome'], 'Assistant report: The list markup is in place locally.\nAssistant report: Production has not been contacted.')
        self.assertEqual(current['open_work'], 'User intent: Keep production deployment pending.')
        self.assertNotIn('deployed to production', current['summary'])
        self.assertIn('User intent: Task LUX-54: replace the carousel.', current['summary'])
        self.assertEqual(current['field_provenance']['summary']['basis'], 'attributed_source_excerpts')
        self.assertEqual(current['field_provenance']['summary']['field_association'], 'model_inference')
        self.assertEqual({ref['source_id'] for ref in current['evidence']['outcome']}, {reported['id']})

    def test_wrong_field_citations_cannot_borrow_support_from_other_fields(self):
        self.remember('Task LUX-54: use a local preview without a public feed. That keeps public readers unaffected. Preserve the audit history. Run regression checks.')
        def mismatched(request):
            spans = json.loads(request['prompt'].split('\n', 1)[1])['events'][0]['spans']
            item = {'summary': 'Local preview with audit history.', 'goal': 'Preserve the audit history.',
                    'rationale': 'The preview has no public feed.', 'outcome': '',
                    'open_work': 'Audit history retention remains required.',
                    'evidence': {'summary': [spans[0]['ref'], spans[2]['ref']],
                                 'goal': [spans[2]['ref']], 'rationale': [spans[1]['ref']],
                                 'outcome': [], 'open_work': [spans[3]['ref']]}}
            return receipt({'episodes': [item], 'claims': [], 'relations': []})
        self.assertEqual(self.process(mismatched)['status'], 'processed')
        current = self.state()['episodes'][0]
        self.assertEqual(current['rationale'], 'User intent: That keeps public readers unaffected.')
        self.assertEqual(current['open_work'], 'User intent: Run regression checks.')
        self.assertEqual(current['goal'], 'User intent: Preserve the audit history.')
        self.assertNotIn('public feed', current['rationale'])
        self.assertNotIn('audit history', current['open_work'])

    def test_direct_commit_expands_sliced_evidence_with_original_negation(self):
        saved = self.remember('Task LUX-54: the staging deployment did not pass; production remains untouched.')
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            job = memory.claim(self.project)
            body = job['sources'][0]['body']
            start = body.index('pass;')
            ref = {'source_id': saved['id'], 'start': start, 'end': start + 4, 'quote': 'pass'}
            output = {'episodes': [{'key': 'unsafe', 'summary': 'Production deployment passed.',
                                   'evidence': {'summary': [ref]}}], 'claims': [], 'relations': []}
            memory.commit(job, output)
            current = memory.snapshot(self.project)['episodes'][0]
        self.assertEqual(current['summary'], 'User intent: ' + body)
        self.assertEqual(current['evidence']['summary'][0]['start'], 0)
        self.assertEqual(current['evidence']['summary'][0]['end'], len(body))
        self.assertEqual(current['evidence']['summary'][0]['quote'], body)

    def test_grounded_field_bound_counts_complete_quote_whitespace(self):
        body = 'Task LUX-54.' + ' ' * 6100 + 'Production remains unchecked.'
        saved = self.remember(body)
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            job = memory.claim(self.project)
            disclosed = job['sources'][0]['body']
            start = disclosed.index('Production')
            ref = {'source_id': saved['id'], 'start': start, 'end': len(disclosed),
                   'quote': disclosed[start:]}
            with self.assertRaisesRegex(ValueError, 'grounded field exceeds'):
                memory.commit(job, {'episodes': [{'key': 'whitespace', 'summary': 'Production remains unchecked.',
                    'evidence': {'summary': [ref]}}]})
            self.assertEqual(memory.snapshot(self.project)['episodes'], [])

    def test_legacy_prior_prose_is_rebuilt_from_its_own_disclosed_evidence(self):
        self.remember('Task LUX-54: keep production deployment pending. Local checks are not complete.')
        def initial(request):
            output = self.standalone(request)
            spans = json.loads(request['prompt'].split('\n', 1)[1])['events'][0]['spans']
            output['output']['episodes'][0].update(outcome='Local checks are not complete.', open_work='Keep production deployment pending.')
            output['output']['episodes'][0]['evidence'].update(outcome=[spans[1]['ref']], open_work=[spans[0]['ref']])
            return output
        self.assertEqual(self.process(initial)['status'], 'processed')
        # Old-database fixture: pre-upgrade prose was model-authored. Public
        # reads and processing below must not launder it as grounded context.
        with Store(self.data) as store:
            old = ProjectMemory(store).snapshot(self.project)['episodes'][0]
            old['outcome'] = 'Production deployment passed.'
            old['open_work'] = 'No work remains.'
            old.pop('field_provenance', None)
            store._connection.execute('UPDATE pm_objects SET data_json=? WHERE id=?', (json.dumps(old), old['id']))
            store._connection.commit()
        self.remember('Continue task LUX-54. Keep the staging check open.', 'two')
        def continue_task(request):
            output = self.standalone(request)
            supplied = json.loads(request['prompt'].split('\n', 1)[1])
            item = output['output']['episodes'][0]
            item['continuation_of'] = supplied['current_state']['episodes'][0]['id']
            item['evidence']['continuation_of'] = [supplied['events'][0]['spans'][0]['ref']]
            return output
        self.assertEqual(self.process(continue_task)['status'], 'processed')
        current = self.state()['episodes'][0]
        self.assertEqual(current['prior_outcome'], 'User intent: Local checks are not complete.')
        self.assertNotIn('No work remains', current['prior_open_work'])
        self.assertEqual(current['prior_context_status'], 'historical_current_validity_unresolved')
        self.assertEqual(current['field_provenance']['prior_outcome']['basis'], 'attributed_source_excerpts')

    def test_retrieval_keeps_complete_attributed_fields_or_reports_omission(self):
        import xml.etree.ElementTree as ET
        from codex_mem.project_memory_retrieval import build_project_brief, render_project_brief
        body = 'Task LUX-54: ' + 'local preview detail ' * 40 + 'but production was not contacted.'
        self.remember(body, source='hook:Stop')
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        with Store(self.data) as store:
            brief = build_project_brief(store, self.project, include_shadow=True)
        rendered = ET.fromstring(render_project_brief(brief, budget=12000))
        field = rendered.find(".//field[@name='summary']")
        self.assertIsNotNone(field)
        self.assertIsNone(field.find('derived_text'))
        self.assertEqual(field.find('source/quote_excerpt').text, body)
        self.assertEqual(field.find('source').attrib['source_role'], 'assistant_report')
        self.assertEqual(field.find('source').attrib['quote_truncated'], 'False')
        self.assertEqual(field.attrib['basis'], 'attributed_source_excerpts')
        self.assertEqual(field.attrib['field_association'], 'model_inference')
        small = ET.fromstring(render_project_brief(brief, budget=1700))
        small_field = small.find(".//field[@name='summary']")
        if small_field is not None:
            self.assertIsNone(small_field.find('derived_text'))
            self.assertEqual(small_field.find('source/quote_excerpt').text, body)
        else:
            self.assertTrue(any(int(item.attrib.get('fields_omitted', 0)) > 0 for item in small.findall('item')))

    def test_grounded_field_keeps_all_exact_sources_together_in_projection_and_budget(self):
        import xml.etree.ElementTree as ET
        from codex_mem.project_memory_retrieval import build_project_brief, render_project_brief
        body = ('Task LUX-54: ' + 'local preview detail ' * 16 + 'production was not contacted. '
                + 'Manual approval ' * 16 + 'remains required.')
        original = self.remember(body, source='hook:Stop')
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        stored = self.state()['episodes'][0]['evidence']['summary']
        self.assertEqual(len(stored), 2)
        for query in ('', 'What is the current status of LUX-54?'):
            with Store(self.data) as store:
                brief = build_project_brief(store, self.project, query=query, include_shadow=True)
            summary = next(field for field in brief['episodes'][0]['fields']
                           if field['name'] in {'summary', 'summary_source_excerpt'})
            self.assertEqual(len(summary['evidence']), 2)
            for budget in (1700, 12000):
                xml = render_project_brief(brief, budget=budget)
                self.assertLessEqual(len(xml), budget)
                rendered = ET.fromstring(xml)
                field = rendered.find(f".//field[@name='{summary['name']}']")
                if field is None:
                    self.assertTrue(any(int(item.attrib.get('fields_omitted', 0)) > 0
                                        for item in rendered.findall('item')))
                    continue
                self.assertIsNone(field.find('derived_text'))
                sources = field.findall('source')
                self.assertEqual(len(sources), 2)
                for source, ref in zip(sources, stored):
                    self.assertEqual(source.attrib['source_id'], original['id'])
                    self.assertEqual(source.attrib['source_role'], 'assistant_report')
                    self.assertEqual(int(source.attrib['start']), ref['start'])
                    self.assertEqual(int(source.attrib['end']), ref['end'])
                    self.assertEqual(source.find('quote_excerpt').text, body[ref['start']:ref['end']])
                    self.assertEqual(source.attrib['quote_truncated'], 'False')

    def test_negated_reopen_cannot_bypass_through_same_task_marker(self):
        result = self.named_identity_reconsideration('LUX-54', 'Task LUX-54: another exporter. Do not reopen LUX-54 as the same task.')
        self.assertNotEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_quoted_reopen_is_not_user_authorization(self):
        result = self.named_identity_reconsideration('LUX-54', 'Task LUX-54: another exporter. "Reopen LUX-54 as the same task."')
        self.assertNotEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_cli_help_backtick_reopen_is_not_user_authorization(self):
        result = self.named_identity_reconsideration('LUX-54', 'Task LUX-54: another exporter. CLI help: `Reopen LUX-54 as the same task.`')
        self.assertNotEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_reopen_question_is_not_user_authorization(self):
        result = self.named_identity_reconsideration('LUX-54', 'Task LUX-54: another exporter. Should we reopen LUX-54 as the same task?')
        self.assertNotEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_reopen_reference_and_distinct_ticket_stay_separate(self):
        result = self.named_identity_reconsideration('LUX-54', 'Task LUX-54: another exporter. A separate ticket references Reopen LUX-54 as the same task.')
        self.assertNotEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_assistant_report_cannot_authorize_reopening_through_same_task_marker(self):
        self.remember('Task LUX-54: build the exporter.')
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        self.remember('Reopen LUX-54 as the same task.', 'two', source='hook:Stop')
        def report(request):
            supplied = json.loads(request['prompt'].split('\n', 1)[1])
            output = self.standalone(request)
            item = output['output']['episodes'][0]
            item['continuation_of'] = supplied['current_state']['episodes'][0]['id']
            item['evidence']['continuation_of'] = [supplied['events'][0]['spans'][0]['ref']]
            return output
        self.assertEqual(self.process(report)['status'], 'processed')
        self.assertEqual(len(self.state()['episodes']), 2)
        self.assertTrue(all(item['parent_episode_id'] is None for item in self.state()['episodes']))

    def test_explicit_reopening_supports_task_name_after_identity_phrase(self):
        result = self.named_identity_reconsideration('MAP-91', 'Reopen the same task MAP-91.')
        self.assertEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 1)

    def test_claim_rationale_keeps_own_evidence_without_changing_claim_state(self):
        self.remember('Keep deployment pending. The local preview does not contact production.')
        def claim(request):
            spans = json.loads(request['prompt'].split('\n', 1)[1])['events'][0]['spans']
            item = {'text': 'Keep deployment pending.', 'kind': 'requirement', 'workflow_state': 'open',
                    'scope': {'environment': '', 'version': '', 'worktree': ''},
                    'rationale': 'Production validation already passed.',
                    'evidence': {'text': [spans[0]['ref']], 'workflow_state': [spans[0]['ref']], 'rationale': [spans[1]['ref']]}}
            return receipt({'episodes': [], 'claims': [item], 'relations': []})
        self.assertEqual(self.process(claim)['status'], 'processed')
        current = self.state()['claims'][0]
        self.assertEqual(current['rationale'], 'User intent: The local preview does not contact production.')
        self.assertEqual(current['text'], 'Keep deployment pending.')
        self.assertEqual(current['workflow_state'], 'open')
        self.assertEqual(current['field_provenance']['rationale']['basis'], 'attributed_source_excerpts')

    def test_tool_excerpt_keeps_local_command_scope_without_production_promotion(self):
        self.remember('Command: local-check --environment staging. Result: passed. Production was not contacted.', source='hook:PostToolUse')
        self.assertEqual(self.process(self.standalone)['status'], 'processed')
        current = self.state()['episodes'][0]
        self.assertIn('Tool record: Command: local-check --environment staging.', current['summary'])
        self.assertIn('Tool record: Production was not contacted.', current['summary'])
        self.assertEqual(current['field_provenance']['summary']['source_roles'], ['tool_record'])

    def test_partial_disclosed_source_is_labelled_without_loading_missing_tail(self):
        self.remember('Task LUX-54: ' + 'local staging detail ' * 40 + 'production was not contacted.')
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            job = memory.claim(self.project, max_chars=256)
            source = job['sources'][0]
            output = {'episodes': [{'key': 'partial', 'summary': 'Everything is deployed.',
                                   'evidence': {'summary': [{'source_id': source['id'], 'start': 0,
                                               'end': len(source['body']), 'quote': source['body']}]}}],
                      'claims': [], 'relations': []}
            memory.commit(job, output)
            current = memory.snapshot(self.project)['episodes'][0]
        self.assertTrue(current['field_provenance']['summary']['partial_context'])
        self.assertTrue(current['summary'].startswith('User intent (partial context): '))
        self.assertNotIn('Everything is deployed', current['summary'])
        self.assertEqual(current['evidence']['summary'][0]['end'], len(source['body']))
        self.assertNotIn('production was not contacted', current['summary'])

    def test_previous_policy_noop_does_not_suppress_new_reconsideration(self):
        import hashlib
        self.split()
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            job = memory.claim(self.project)
            memory.commit(job, {'episodes': [], 'claims': [], 'relations': []})
            # Existing-database fixture with the pre-appositive v2 fingerprint.
            old_input = ['task-consolidation-v2', job['project'], job['deletion_generation'], 32, 24000,
                         job['reconsideration']['candidate_head_ids']]
            fingerprint = hashlib.sha256(json.dumps(old_input, ensure_ascii=False, sort_keys=True,
                                                     separators=(',', ':')).encode()).hexdigest()
            store._connection.execute('UPDATE pm_jobs SET id=?,fingerprint=? WHERE id=?',
                                      ('pmj_' + fingerprint[:40], fingerprint, job['job_id']))
            store._connection.commit()
        result = self.process(self.consolidate)
        self.assertEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 1)
        self.assertEqual(self.process(lambda request: self.fail('completed work replayed'))['status'], 'idle')

    def test_complete_excerpt_over_field_budget_is_rejected_without_state_loss(self):
        self.remember('Task LUX-54: ' + 'x' * 5980 + '.')
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            job = memory.claim(self.project)
            source = job['sources'][0]
            output = {'episodes': [{'key': 'oversize', 'summary': 'Short unsupported conclusion.',
                                   'evidence': {'summary': [{'source_id': source['id'], 'start': 0,
                                               'end': len(source['body']), 'quote': source['body']}]}}],
                      'claims': [], 'relations': []}
            with self.assertRaisesRegex(ValueError, 'grounded field exceeds'):
                memory.commit(job, output)
            self.assertEqual(memory.snapshot(self.project)['episodes'], [])

    def test_russian_explicit_reopening_names_the_original_task(self):
        result = self.named_identity_reconsideration('LUX-54', 'Переоткрой задачу LUX-54.')
        self.assertEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 1)

    def test_russian_negative_reopening_cannot_use_same_task_fallback(self):
        result = self.named_identity_reconsideration('LUX-54', 'Задача LUX-54: экспортер. Не переоткрывай LUX-54, это та же задача.')
        self.assertNotEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_russian_reopening_question_keeps_task_separate(self):
        result = self.named_identity_reconsideration('LUX-54', 'Задача LUX-54: экспортер. Переоткрой задачу LUX-54?')
        self.assertNotEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_russian_quoted_reopening_keeps_task_separate(self):
        result = self.named_identity_reconsideration('LUX-54', 'Задача LUX-54: экспортер. «Переоткрой задачу LUX-54.»')
        self.assertNotEqual(result['status'], 'processed', result)
        self.assertEqual(len(self.state()['episodes']), 2)

    def test_source_assembly_preserves_total_output_bound_atomically(self):
        self.remember('Task LUX-54: ' + 'x' * 4800 + '.')
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            job = memory.claim(self.project)
            source = job['sources'][0]
            ref = {'source_id': source['id'], 'start': 0, 'end': 4, 'quote': 'Task'}
            output = {'episodes': [{'key': f'episode-{index}', 'summary': 'Task',
                                   'evidence': {'summary': [ref]}} for index in range(24)],
                      'claims': [], 'relations': []}
            with self.assertRaisesRegex(ValueError, 'assembled project memory output exceeds limit'):
                memory.commit(job, output)
            self.assertEqual(memory.snapshot(self.project)['episodes'], [])

    def test_reopening_requires_unconditional_positive_user_command(self):
        rejected = (
            "Don't reopen LUX-54 as the same task.",
            'Avoid reopening LUX-54 as the same task.',
            '```\nReopen LUX-54 as the same task.\n```',
            'Reopen LUX-54 as the same task is forbidden.',
            'Reopen LUX-54 as the same task only after approval.',
            'There is no need to reopen LUX-54 as the same task.',
            'Proceed without reopening LUX-54 as the same task.',
            'We are not ready to reopen LUX-54 as the same task.',
            'Нельзя переоткрывать LUX-54, это та же задача.',
            'Запрещено переоткрой задачу LUX-54.',
            'Переоткрой задачу LUX-54 без согласования.',
            'Переоткрой задачу LUX-54 только после согласования.',
        )
        for index, sentence in enumerate(rejected):
            with self.subTest(sentence=sentence):
                self.project = Path(self.temp.name) / f'negative-{index}'
                self.project.mkdir()
                self.data = Path(self.temp.name) / f'data-{index}'
                configure(self.data, capture_scope='all', project_memory_mode='shadow',
                          project_memory_projects=[str(self.project)], jev_quality_enabled=False)
                result = self.named_identity_reconsideration('LUX-54', 'Task LUX-54: exporter. ' + sentence)
                self.assertNotEqual(result['status'], 'processed', result)
                self.assertEqual(len(self.state()['episodes']), 2)

    def test_affirmative_reopening_commands_keep_common_identity_word_orders(self):
        accepted = ('Reopen LUX-54.', 'Please reopen the same ticket LUX-54.',
                    'Reopen LUX-54, the same exporter task.',
                    'Пожалуйста, повторно открой LUX-54, ту же задачу.')
        for index, sentence in enumerate(accepted):
            with self.subTest(sentence=sentence):
                self.project = Path(self.temp.name) / f'positive-{index}'
                self.project.mkdir()
                self.data = Path(self.temp.name) / f'positive-data-{index}'
                configure(self.data, capture_scope='all', project_memory_mode='shadow',
                          project_memory_projects=[str(self.project)], jev_quality_enabled=False)
                result = self.named_identity_reconsideration('LUX-54', sentence)
                self.assertEqual(result['status'], 'processed', result)
                self.assertEqual(len(self.state()['episodes']), 1)

    def test_explicit_reopening_accepts_bounded_same_description_appositive(self):
        for index, (identity, sentence) in enumerate((
            ('MS-81', 'Reopen MS-81, the same timing repair.'),
            ('RIV-42', 'Please reopen RIV-42, same invoice layout correction.'),
        )):
            with self.subTest(sentence=sentence):
                self.project = Path(self.temp.name) / f'appositive-{index}'
                self.project.mkdir()
                self.data = Path(self.temp.name) / f'appositive-data-{index}'
                configure(self.data, capture_scope='all', project_memory_mode='shadow',
                          project_memory_projects=[str(self.project)], jev_quality_enabled=False)
                result = self.named_identity_reconsideration(identity, sentence)
                self.assertEqual(result['status'], 'processed', result)
                self.assertEqual(len(self.state()['episodes']), 1)

    def test_same_description_reopening_rejects_qualified_or_reference_commands(self):
        rejected = (
            'Reopen LUX-54, the same repair only after approval.',
            'Reopen LUX-54, the same repair pending approval.',
            'Reopen LUX-54, the same command example.',
            'Reopen LUX-54, the same repair perhaps.',
            'Reopen LUX-54, the same repair which needs approval.',
            'Reopen LUX-54, the same repair when approved.',
            'Reopen LUX-54, the same timing repair is forbidden.',
            'Reopen LUX-54, the same timing repair might be needed.',
            'Reopen LUX-54, the same repair might return.',
            'Reopen LUX-54, the same timing repair reference.',
            'Reopen LUX-54, the same timing repair, only after approval.',
            '"Reopen LUX-54, the same timing repair."',
            'Reopen LUX-54, the same timing repair?',
            'Reopen OTHER-82, the same timing repair.',
            'A separate task: Reopen LUX-54, the same timing repair.',
            'Reopen LUX-54, the same one two three four five.',
        )
        for index, sentence in enumerate(rejected):
            with self.subTest(sentence=sentence):
                self.project = Path(self.temp.name) / f'qualified-appositive-{index}'
                self.project.mkdir()
                self.data = Path(self.temp.name) / f'qualified-appositive-data-{index}'
                configure(self.data, capture_scope='all', project_memory_mode='shadow',
                          project_memory_projects=[str(self.project)], jev_quality_enabled=False)
                result = self.named_identity_reconsideration('LUX-54', 'Task LUX-54: exporter. ' + sentence)
                self.assertNotEqual(result['status'], 'processed', result)
                self.assertEqual(len(self.state()['episodes']), 2)

    def test_sliced_appositive_cannot_hide_original_prohibition_or_condition(self):
        for index, sentence in enumerate(('Do not Reopen LUX-54, the same timing repair.',
                                           'Reopen LUX-54, the same timing repair only after approval.')):
            with self.subTest(sentence=sentence):
                self.project = Path(self.temp.name) / f'sliced-appositive-{index}'
                self.project.mkdir()
                self.data = Path(self.temp.name) / f'sliced-appositive-data-{index}'
                configure(self.data, capture_scope='all', project_memory_mode='shadow',
                          project_memory_projects=[str(self.project)], jev_quality_enabled=False)
                for session, body in (('one', 'Task LUX-54: build exporter.'),
                                      ('two', 'Task LUX-54: exporter. ' + sentence)):
                    self.remember(body, session)
                    self.assertEqual(self.process(self.standalone)['status'], 'processed')
                with Store(self.data) as store:
                    memory = ProjectMemory(store)
                    job = memory.claim(self.project)
                    self.assertIsNotNone(job)
                    sources = job['sources']
                    source = next(s for s in sources if sentence in s['body'])
                    quote = 'Reopen LUX-54, the same timing repair'
                    start = source['body'].index(quote)
                    sliced = {'source_id': source['id'], 'quote': quote,
                              'start': start, 'end': start + len(quote)}
                    refs = [{'source_id': s['id'], 'start': 0, 'end': len(s['body']), 'quote': s['body']}
                            for s in sources]
                    heads = job['state']['episodes']
                    proposal = {'episodes': [{'key': 'sliced-reopening', 'summary': 'Task LUX-54.', 'goal': 'Task LUX-54.',
                        'continuation_of': heads[0]['id'], 'consolidation_of': [heads[1]['id']],
                        'evidence': {'summary': refs, 'goal': refs, 'continuation_of': [sliced],
                                     'consolidation_of': [sliced]}}]}
                    with self.assertRaisesRegex(ValueError, 'consolidation needs a distinct continued head'):
                        memory.commit(job, proposal)
                    self.assertEqual(len(memory.snapshot(self.project)['episodes']), 2)
