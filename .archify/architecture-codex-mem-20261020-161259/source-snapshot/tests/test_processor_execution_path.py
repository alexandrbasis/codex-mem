"""Host capture, real queues, external execution and retrieval in one process path."""
import json
import unittest
from unittest import mock

import test_execution_contract as fixture
from codex_mem.config import configure
from codex_mem.hooks import handle_hook
from codex_mem.processor import process_pending
from codex_mem.episode_processor import process_project_memory
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store


class ProcessorExecutionPathTests(unittest.TestCase):
    scenario = fixture.ExecutionContractTests.scenario

    def setUp(self):
        fixture.ExecutionContractTests.setUp(self)
        self.data = self.root / 'memory'
        self.project = self.root / 'project'
        self.project.mkdir()
        configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                  project_memory_mode='pilot', project_memory_projects=[str(self.project)])

    def capture(self, event='UserPromptSubmit', **extra):
        with Store(self.data) as store:
            handle_hook(dict(hook_event_name=event, cwd=str(self.project), session_id='source-session',
                             turn_id='source-turn', **extra), store)

    def context(self):
        with Store(self.data) as store:
            return handle_hook(dict(hook_event_name='SessionStart', source='startup', cwd=str(self.project),
                                    session_id='reader-session'), store)

    def test_unavailable_codex_auth_preserves_work_before_dispatch(self):
        self.capture(prompt='Use local import.')
        self.scenario(account={'account': None, 'requiresOpenaiAuth': True})
        result = process_pending(self.project, self.data, codex=str(self.executable), timeout=10)
        self.assertEqual('failed', result['status'], result)
        with Store(self.data) as store:
            job = store.observation_job_status(self.project, result['job_id'])
            self.assertEqual('authentication_unavailable', job['failure_receipts'][-1]['reason_code'])
            self.assertEqual(1, job['source_count'])
            self.assertEqual([], store.search(self.project, 'import'))
        self.assertNotIn('turn/start', self.executable.with_suffix('.trace').read_text())

    def test_observation_and_summary_survive_capture_execution_and_retrieval(self):
        self.capture('Stop', last_assistant_message='Use local import.')
        summary = dict(title='Import choice', request=None, investigated='', learned='Use local import.',
                       completed='', next_steps='', notes='', source_ids=['s1'])
        note = dict(title='Import choice', body='Use local import.', tags=[], source_ids=['s1'],
                    observation=dict(type='discovery', subtitle='', narrative='Use local import.', facts=[],
                                     concepts=[], files_read=[], files_modified=[]))
        self.scenario(output={'result': dict(notes=[note], disposition='processed', session_summary=summary)})
        result = process_pending(self.project, self.data, codex=str(self.executable), timeout=10)
        self.assertEqual('processed', result['status'], result)
        self.assertEqual(1, result['session_summary_count'])
        self.assertIn('Use local import.', json.dumps(self.context()))
        with Store(self.data) as store:
            self.assertEqual('idle', process_pending(self.project, self.data, codex=str(self.executable))['status'])
            self.assertEqual([], store.search(self.root / 'other', 'Import'))

    def test_project_memory_keeps_original_role_and_citations_in_next_context(self):
        self.capture(prompt='Use local import.')
        self.scenario(mode='project_memory')
        with mock.patch('codex_mem.processor.shutil.which', return_value=str(self.executable)):
            result = process_project_memory(self.project, self.data, timeout=10)
        self.assertEqual('processed', result['status'], result)
        with Store(self.data) as store:
            snapshot = ProjectMemory(store).snapshot(self.project)
            self.assertIn('Use local import.', snapshot['episodes'][0]['summary'])
            evidence = snapshot['episodes'][0]['evidence']['summary'][0]
            self.assertEqual('user_intent', evidence['source_role'])
            self.assertTrue(evidence['source_id'])
            self.assertEqual(12, ProjectMemory(store).attempts(self.project)[0]['total_tokens'])
        self.assertIn('Use local import.', json.dumps(self.context()))

    def test_failed_execution_cannot_write_partial_memory_in_either_processor(self):
        for processor in ('observation', 'project_memory'):
            for mode in ('cancelled', 'invalid_json', 'invalid_schema', 'provider_error', 'disconnect', 'timeout'):
                with self.subTest(processor=processor, mode=mode):
                    self.data = self.root / (processor + '-' + mode)
                    configure(self.data, capture_scope='all', service_enabled=False, semantic_enabled=False,
                              project_memory_mode='pilot', project_memory_projects=[str(self.project)])
                    self.capture(prompt='Use local import.')
                    self.scenario(mode=mode, **({'output': {}} if mode == 'invalid_schema' else {}))
                    timeout = 2 if mode == 'timeout' else 10
                    if processor == 'observation':
                        result = process_pending(self.project, self.data, codex=str(self.executable), timeout=timeout)
                    else:
                        with mock.patch('codex_mem.processor.shutil.which', return_value=str(self.executable)):
                            result = process_project_memory(self.project, self.data, timeout=timeout)
                    self.assertEqual('failed', result['status'], result)
                    with Store(self.data) as store:
                        state = ProjectMemory(store).snapshot(self.project)
                        self.assertEqual([], state['episodes'])
                        self.assertGreater(state['coverage']['pending_events'], 0)
                        self.assertEqual([], [item for item in store.search(self.project, 'Import')
                                              if item['source'].startswith('processor:')])
                        if processor == 'project_memory':
                            attempt = ProjectMemory(store).attempts(self.project)[0]
                            self.assertEqual(12, attempt['total_tokens'])
                            ProjectMemory(store).retry_failed(self.project, include_quarantined=True)
                    # Explicit retries use the retained source and can complete normally.
                    if processor == 'observation':
                        self.scenario()
                        retry = process_pending(self.project, self.data, codex=str(self.executable),
                                                retry_failed=True, timeout=10)
                        self.assertEqual('skipped', retry['status'], retry)
                    else:
                        self.scenario(mode='project_memory')
                        with mock.patch('codex_mem.processor.shutil.which', return_value=str(self.executable)):
                            retry = process_project_memory(self.project, self.data, timeout=10)
                        self.assertEqual('processed', retry['status'], retry)
