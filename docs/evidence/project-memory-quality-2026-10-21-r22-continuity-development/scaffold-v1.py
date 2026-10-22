"""Prospective public processor/Store controls; no native calls or old trial inputs.

Run: python3 -m unittest discover -s docs/evidence/project-memory-quality-2026-10-21-r22-continuity-development -p 'test_*.py' -v
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from codex_mem import episode_processor as ep
from codex_mem.config import configure
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store


def receipt(output):
    return {'output': output,
            'evidence': {'thread_start': {'thread_id': 'public-worker', 'model': ep.MODEL,
                'reasoning_effort': ep.REASONING_EFFORT, 'model_provider': 'openai'},
                'turn_started': {'thread_id': 'public-worker', 'turn_id': 'public-turn'},
                'turn_completed': True, 'no_tools': True, 'rerouted': False},
            'metrics': {'duration_ms': 1, 'usage': {'tokens': {'input_tokens': 100, 'output_tokens': 10}}}}


def payload(request):
    return json.loads(request['prompt'].split('\n', 1)[1])


def episode(span, target=None, consolidate=()):
    return {'summary': span['text'], 'goal': span['text'] if target is None else '',
            'rationale': '', 'outcome': '', 'open_work': '',
            'continuation_of': target, 'consolidation_of': list(consolidate),
            'evidence': {'summary': [span['ref']],
                'goal': [span['ref']] if target is None else [],
                'rationale': [], 'outcome': [], 'open_work': [],
                'continuation_of': [span['ref']] if target else [],
                'consolidation_of': [span['ref']] if consolidate else []}}


class ContinuationSlotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='public-continuation-')
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'workspace'
        self.project.mkdir()
        self.data = Path(self.temp.name) / 'data'
        configure(self.data, capture_scope='all', project_memory_mode='shadow',
                  project_memory_projects=[str(self.project)], jev_quality_enabled=False)

    def remember(self, text, session='next', project=None):
        with Store(self.data) as store:
            return store.remember(project or self.project, 'Public observation', text,
                                  source='hook:UserPromptSubmit', session_id=session)

    def state(self):
        with Store(self.data) as store:
            return ProjectMemory(store).snapshot(self.project, include_episode_history=True)

    def process(self, runner, project=None):
        return ep.process_project_memory(project or self.project, self.data, runner=runner)

    def seed(self, text='TASK-281: Prepare the index.', session='initial', project=None):
        source = self.remember(text, session, project)
        def runner(request):
            spans = payload(request)['events'][0]['spans']
            return receipt({'episodes': [episode(span) for span in spans], 'claims': []})
        self.assertEqual(self.process(runner, project)['status'], 'processed')
        return source

    def head(self):
        return next(item for item in self.state()['episodes'] if item['is_current_head'])

    def domain(self):
        state = self.state()
        return {key: state[key] for key in ('revision', 'episodes', 'claims', 'relations')}

    def runner(self, target, reverse=False, both_supported=False):
        def run(request):
            supplied = payload(request)
            self.assertEqual(supplied['job_kind'], 'events')
            fresh = [event for event in supplied['events'] if not event.get('historical')]
            spans = fresh[-1]['spans']
            records = [episode(span, target) for span in spans]
            self.assertEqual(len(records), 2)
            if reverse:
                records.reverse()
            return receipt({'episodes': records, 'claims': []})
        return run

    def assert_rejected_without_domain_commit(self, runner):
        before = self.domain()
        pending = self.state()['coverage']['pending_events']
        result = self.process(runner)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['code'], 'invalid_response', result)
        self.assertEqual(self.domain(), before)
        self.assertEqual(self.state()['coverage']['pending_events'], pending)
        # Attempt/failure accounting is expected to change. No proposed object,
        # relation, revision or source watermark may be partially committed.
        self.assertEqual(self.process(lambda _: self.fail('quarantine unexpectedly retried'))['status'], 'idle')

    def test_two_unsupported_requests_do_not_consume_a_continuation_slot(self):
        self.seed()
        prior = self.head()
        self.remember('The shelf is blue. The cabinet is narrow.')
        result = self.process(self.runner(prior['id']))
        self.assertEqual(result['status'], 'processed', result)
        self.assertEqual(result['omitted_continuities_count'], 2)
        state = self.state()
        children = [item for item in state['episodes'] if item['id'] != prior['id']]
        self.assertEqual(len(children), 2)
        self.assertTrue(all(item['parent_episode_id'] is None for item in children))
        self.assertTrue(all(item['logical_episode_id'] == item['id'] for item in children))
        self.assertTrue(next(item for item in state['episodes'] if item['id'] == prior['id'])['is_current_head'])
        self.assertEqual(state['coverage']['pending_events'], 0)

    def mixed(self, reverse):
        self.seed()
        prior = self.head()
        self.remember('Continue TASK-281: inspect the revised index. The cabinet is narrow.')
        result = self.process(self.runner(prior['id'], reverse=reverse))
        self.assertEqual(result['status'], 'processed', result)
        self.assertEqual(result['omitted_continuities_count'], 1)
        state = self.state()
        successors = [item for item in state['episodes'] if item['parent_episode_id'] == prior['id']]
        self.assertEqual(len(successors), 1)
        self.assertEqual(successors[0]['logical_episode_id'], prior['logical_episode_id'])
        self.assertEqual(successors[0]['episode_version'], 2)
        self.assertEqual(len([item for item in state['episodes'] if item['id'] != prior['id'] and item['parent_episode_id'] is None]), 1)
        self.assertFalse(next(item for item in state['episodes'] if item['id'] == prior['id'])['is_current_head'])

    def test_supported_then_unsupported_uses_only_one_slot(self):
        self.mixed(False)

    def test_unsupported_then_supported_uses_only_one_slot(self):
        self.mixed(True)

    def test_two_supported_same_root_reject_whole_batch(self):
        self.seed()
        target = self.head()['id']
        self.remember('Continue TASK-281: inspect the index. Continue TASK-281: revise its format.')
        self.assert_rejected_without_domain_commit(self.runner(target))

    def test_distinct_supported_roots_succeed(self):
        self.seed('TASK-281: Prepare the index. TASK-639: Prepare the schedule.')
        heads = self.state()['episodes']
        by_task = {task: next(item for item in heads if task in item['goal']) for task in ('TASK-281', 'TASK-639')}
        self.remember('Continue TASK-281: inspect the index. Continue TASK-639: inspect the schedule.')
        def run(request):
            spans = [e for e in payload(request)['events'] if not e.get('historical')][-1]['spans']
            return receipt({'episodes': [episode(span, by_task[task]['id']) for span, task in zip(spans, by_task)], 'claims': []})
        result = self.process(run)
        self.assertEqual(result['status'], 'processed', result)
        current = [item for item in self.state()['episodes'] if item['is_current_head']]
        self.assertEqual({item['parent_episode_id'] for item in current}, {item['id'] for item in heads})

    def split_heads(self):
        self.seed()
        self.seed('Continue TASK-281: inspect the index.', session='second')
        self.assertEqual(len([item for item in self.state()['episodes'] if item['is_current_head']]), 2)

    def consolidation_runner(self, overlap):
        def run(request):
            supplied = payload(request)
            self.assertEqual(supplied['job_kind'], 'reconsideration')
            heads = supplied['current_state']['episodes']
            self.assertEqual(len(heads), 2)
            span = next(span for event in supplied['events'] for span in event['spans'] if span['text'].strip().startswith('Continue TASK-281'))
            first = episode(span, heads[0]['id'], [heads[1]['id']])
            records = [first]
            if overlap:
                records.append(episode(span, heads[1]['id'], [heads[0]['id']]))
            return receipt({'episodes': records, 'claims': []})
        return run

    def test_valid_reconsideration_still_consolidates_two_heads(self):
        self.split_heads()
        result = self.process(self.consolidation_runner(False))
        self.assertEqual(result['status'], 'processed', result)
        self.assertEqual(len([item for item in self.state()['episodes'] if item['is_current_head']]), 1)

    def test_reconsideration_overlap_rejects_whole_batch(self):
        self.split_heads()
        self.assert_rejected_without_domain_commit(self.consolidation_runner(True))

    def test_foreign_head_is_not_disclosed_or_accepted(self):
        other = Path(self.temp.name) / 'other'
        other.mkdir()
        configure(self.data, project_memory_projects=[str(self.project), str(other)])
        self.seed(project=other)
        with Store(self.data) as store:
            foreign = ProjectMemory(store).snapshot(other)['episodes'][0]['id']
        self.remember('Continue TASK-281: inspect the index.')
        def run(request):
            supplied = payload(request)
            self.assertNotIn(foreign, request['prompt'])
            return receipt({'episodes': [episode(supplied['events'][0]['spans'][0], foreign)], 'claims': []})
        self.assert_rejected_without_domain_commit(run)

    def test_deleted_head_is_not_resurrected(self):
        self.seed()
        prior = self.head()['id']
        with Store(self.data) as store:
            ProjectMemory(store).forget(self.project, [prior])
        self.remember('Continue TASK-281: inspect the index.')
        def run(request):
            self.assertNotIn(prior, request['prompt'])
            return receipt({'episodes': [episode(payload(request)['events'][0]['spans'][0], prior)], 'claims': []})
        self.assert_rejected_without_domain_commit(run)

    def test_superseded_head_is_not_accepted_as_current(self):
        self.seed()
        old = self.head()['id']
        self.remember('Continue TASK-281: inspect the index.')
        def successor(request):
            span = [e for e in payload(request)['events'] if not e.get('historical')][-1]['spans'][0]
            return receipt({'episodes': [episode(span, old)], 'claims': []})
        self.assertEqual(self.process(successor)['status'], 'processed')
        self.remember('Continue TASK-281: inspect the revised index.', session='last')
        self.assert_rejected_without_domain_commit(successor)


if __name__ == '__main__':
    unittest.main()
