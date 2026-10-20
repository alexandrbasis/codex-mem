"""Explicit project-state reads and bounded process command contracts."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem.cli import main
from codex_mem.config import configure
from codex_mem.mcp import ArgumentError, MemoryMCPServer, TOOLS
from codex_mem.store import Store
from codex_mem.project_memory import ProjectMemory


class ProjectMemoryCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / 'data'
        self.project = str(self.root / 'project')
        configure(self.data, capture_scope='all', project_memory_mode='shadow', project_memory_projects=[self.project])

    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['--data-dir', str(self.data), *args])
        return code, json.loads(output.getvalue())

    def test_config_flags_and_set_list(self):
        code, result = self.cli('config', '--project-memory-mode', 'pilot', '--project-memory-project', self.project,
                                '--project-memory-project', str(self.root / 'second'))
        self.assertEqual(0, code)
        self.assertEqual('pilot', result['project_memory_mode'])
        self.assertEqual(2, len(result['project_memory_projects']))
        code, result = self.cli('config', '--set', 'project_memory_projects=[]')
        self.assertEqual(0, code)
        self.assertEqual([], result['project_memory_projects'])

    def test_cli_status_metadata_and_brief_include_shadow_sources(self):
        with Store(self.data) as store:
            store.remember(self.project, 'User prompt', 'Deployment is still open.', kind='session',
                           source='hook:UserPromptSubmit', session_id='s')
        code, status = self.cli('project-memory', 'status', '--project', self.project)
        self.assertEqual(0, code)
        self.assertTrue(status['enabled'])
        self.assertEqual('shadow', status['mode'])
        self.assertEqual(1, status['coverage']['pending_events'])
        self.assertNotIn('records', status)
        code, brief = self.cli('project-memory', 'brief', '--project', self.project, '--budget', '500')
        self.assertEqual(0, code)
        self.assertTrue(brief['enabled'])
        self.assertLessEqual(len(brief['xml']), 500)
        self.assertTrue(brief['records'][0]['source_ids'])
        self.assertFalse((self.data / 'service-state.json').exists())

    def test_cli_process_native_entrypoint_and_exit_codes(self):
        with mock.patch('codex_mem.episode_processor.process_project_memory', return_value={'status': 'processed'}) as process:
            code, result = self.cli('project-memory', 'process', '--project', self.project, '--timeout', '20')
            self.assertEqual(0, code)
            process.assert_called_once_with(self.project, str(self.data), timeout=20)
        with mock.patch('codex_mem.episode_processor.process_project_memory', return_value={'status': 'failed'}):
            self.assertEqual(2, self.cli('project-memory', 'process', '--project', self.project)[0])

    def test_cli_process_off_never_starts_model(self):
        configure(self.data, project_memory_mode='off')
        with mock.patch('codex_mem.episode_processor.NativeProcessorRunner') as runner:
            code, receipt = self.cli('project-memory', 'process', '--project', self.project)
            self.assertEqual(0, code)
            self.assertEqual('disabled', receipt['status'])
            runner.assert_not_called()

    def test_retry_scopes_failed_and_explicit_quarantine_without_model(self):
        other = str(self.root / 'other')
        configure(self.data, project_memory_projects=[self.project, other])
        with Store(self.data) as store:
            memory = ProjectMemory(store)
            jobs = []
            for project, session, reason in ((self.project, 'failed', 'timeout'),
                                             (self.project, 'quarantined', 'invalid_response'),
                                             (other, 'foreign', 'timeout')):
                store.remember(project, 'Prompt', 'Keep deployment open.', kind='session',
                               source='hook:UserPromptSubmit', session_id=session)
                snapshot = memory.claim(project)
                memory.fail(snapshot, reason)
                jobs.append(snapshot['job_id'])
        with mock.patch('codex_mem.episode_processor.NativeProcessorRunner') as runner:
            code, receipt = self.cli('project-memory', 'retry', '--project', self.project)
            self.assertEqual(0, code)
            self.assertEqual(1, receipt['eligible'])
            with Store(self.data) as store:
                states = dict(store._connection.execute('SELECT id,status FROM pm_jobs'))
            self.assertEqual(['stale', 'quarantined', 'failed'], [states[job] for job in jobs])
            code, receipt = self.cli('project-memory', 'retry', '--project', self.project, '--include-quarantined')
            self.assertEqual(1, receipt['eligible'])
            with Store(self.data) as store:
                states = dict(store._connection.execute('SELECT id,status FROM pm_jobs'))
            self.assertEqual(['stale', 'stale', 'failed'], [states[job] for job in jobs])
            runner.assert_not_called()

    def test_retry_off_does_not_release_jobs(self):
        configure(self.data, project_memory_mode='off')
        with mock.patch('codex_mem.project_memory.ProjectMemory.retry_failed') as retry:
            code, receipt = self.cli('project-memory', 'retry', '--project', self.project, '--include-quarantined')
            self.assertEqual('disabled', receipt['status'])
            self.assertEqual(0, receipt['eligible'])
            retry.assert_not_called()

    def test_service_refreshes_clock_after_episode_before_ordinary_lease(self):
        from codex_mem.service import run_service
        now = [1000.0]
        def episode(*args, **kwargs):
            now[0] += 240
            return None
        with mock.patch('codex_mem.service._run_project_memory_cycle', side_effect=episode), \
             mock.patch('codex_mem.service._claim_due_project', return_value={'kind': 'wait'}) as claim:
            run_service(self.data, processor=lambda *a, **kw: {'status': 'idle'},
                        clock=lambda: now[0], sleeper=lambda delay: None, max_cycles=1)
        self.assertEqual(1240, claim.call_args.args[1])

    def test_mcp_read_only_explicit_shadow_and_off_no_state(self):
        tool = next(tool for tool in TOOLS if tool.name == 'memory_project_state')
        self.assertTrue(tool.annotations['readOnlyHint'])
        self.assertEqual(['project'], tool.input_schema['required'])
        with Store(self.data) as store:
            store.remember(self.project, 'Prompt', 'Keep deployment open.', kind='session', source='hook:UserPromptSubmit')
            server = MemoryMCPServer(store=store)
            brief = server._execute_tool('memory_project_state', {'project': self.project})
            self.assertTrue(brief['enabled'])
            self.assertTrue(brief['records'])
            configure(self.data, project_memory_mode='off')
            brief = server._execute_tool('memory_project_state', {'project': self.project})
            self.assertFalse(brief['enabled'])
            self.assertEqual([], brief['records'])
            self.assertEqual({}, brief['coverage'])

    def test_mcp_arguments_and_privacy(self):
        with Store(self.data) as store:
            server = MemoryMCPServer(store=store)
            for args in ({}, {'project': 'relative'}, {'project': self.project, 'budget': True},
                         {'project': self.project, 'budget': 127}, {'project': self.project, 'query': 1},
                         {'project': self.project, 'unknown': True}):
                with self.subTest(args=args), self.assertRaises(ArgumentError):
                    server._execute_tool('memory_project_state', args)
            configure(self.data, excluded_projects=[self.project])
            brief = server._execute_tool('memory_project_state', {'project': self.project})
            self.assertFalse(brief['enabled'])
            self.assertEqual([], brief['claims'])
