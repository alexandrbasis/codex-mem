"""Public diagnostics distinguish configuration, native preflight and past attempts."""
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_mem.cli import main
from codex_mem.config import configure
from codex_mem.execution import ExecutionProfile
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store


class ExecutionDiagnosticsTests(unittest.TestCase):
    def test_doctor_preserves_historical_requested_identity_before_and_after_migration(self):
        import sqlite3
        from tests.test_project_memory_cost_report import attempt
        from codex_mem.observer_usage_store import _install_schema
        with tempfile.TemporaryDirectory() as directory:
            project = str(Path(directory) / 'project')
            configure(directory, capture_scope='all')
            with Store(directory) as store:
                store.remember(project, 'Raw', 'Keep work open.', source='hook:UserPromptSubmit')
                store.claim_observation_batch(project, 'fixture', 'historical-observer', 'medium')
                _install_schema(store._connection)
            path = Path(directory) / 'memory.sqlite3'
            with sqlite3.connect(path) as connection:
                for column in ('requested_runtime', 'requested_provider', 'requested_model', 'requested_reasoning_effort', 'requested_timeout', 'runtime', 'provider', 'actual_model', 'actual_reasoning_effort', 'profile_basis', 'execution_receipt', 'reason_code'):
                    connection.execute('ALTER TABLE pm_usage_attempts DROP COLUMN ' + column)
                historical = attempt(project=project, model='historical-pm')
                connection.execute('INSERT INTO pm_usage_attempts (' + ','.join(historical) + ') VALUES (' + ','.join('?' for _ in historical) + ')', tuple(historical.values()))
                connection.execute('ALTER TABLE observer_usage_attempts DROP COLUMN requested_model')
                connection.execute("INSERT INTO observer_usage_attempts(job_id,attempt_count,started_at,outcome) SELECT id,1,created_at,'failed' FROM observation_jobs")
            for migrated in (False, True):
                with self.subTest(migrated=migrated):
                    if migrated:
                        with Store(directory):
                            pass
                    before = path.read_bytes()
                    output = io.StringIO()
                    with patch.dict(os.environ, PATH=''), redirect_stdout(output):
                        self.assertEqual(0, main(['--data-dir', directory, 'doctor']))
                    rows = json.loads(output.getvalue())['execution']['latest_attempts']
                    self.assertEqual({'historical-observer', 'historical-pm'}, {row['requested_model'] for row in rows})
                    self.assertTrue(all(row['actual_model'] is None for row in rows))
                    self.assertTrue(all(row['profile_basis'].startswith('legacy_requested') for row in rows))
                    self.assertEqual(before, path.read_bytes())

    def test_latest_refusal_uses_completion_time_across_both_ledgers(self):
        import sqlite3
        from tests.test_project_memory_cost_report import attempt
        from codex_mem.observer_usage_store import _install_schema
        with tempfile.TemporaryDirectory() as directory:
            with Store(directory) as store:
                _install_schema(store._connection)
            path = Path(directory) / 'memory.sqlite3'
            with sqlite3.connect(path) as connection:
                for identifier, start, finish in [('later-finish', '10:00', '10:04'), ('later-start', '10:01', '10:02')]:
                    row = attempt(identifier, outcome='failed', error_code='timeout', started_at='2026-10-23T' + start + ':00Z', finished_at='2026-10-23T' + finish + ':00Z')
                    connection.execute('INSERT INTO pm_usage_attempts (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
                connection.execute("INSERT INTO observer_usage_attempts(job_id,attempt_count,started_at,finished_at,outcome,error_code) VALUES ('observer',1,'2026-10-23T10:03:00Z','2026-10-23T10:03:30Z','failed','timeout')")
            def read():
                output = io.StringIO()
                with patch.dict(os.environ, PATH=''), redirect_stdout(output):
                    self.assertEqual(0, main(['--data-dir', directory, 'doctor']))
                return json.loads(output.getvalue())['execution']['latest_failure']
            before = path.read_bytes()
            row = read()
            self.assertEqual('later-finish', row.get('attempt_id'))
            self.assertEqual('2026-10-23T10:04:00Z', row['failure_recorded_at'])
            self.assertEqual('finished_at', row['failure_time_basis'])
            self.assertEqual(before, path.read_bytes())
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE observer_usage_attempts SET finished_at='2026-10-23T10:05:00Z'")
            self.assertEqual('observation', read()['processor'])
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE observer_usage_attempts SET finished_at=NULL, started_at='2026-10-23T10:06:00Z'")
            row = read()
            self.assertEqual('2026-10-23T10:06:00Z', row['failure_recorded_at'])
            self.assertEqual('started_at_fallback', row['failure_time_basis'])
            with sqlite3.connect(path) as connection:
                from codex_mem.observation_diagnostics import record_failure_receipt
                record_failure_receipt(connection, 'observer', 1, 'model_unavailable', 'exact_model_unavailable', '2026-10-23T10:07:00Z')
            row = read()
            self.assertEqual('2026-10-23T10:07:00Z', row['failure_recorded_at'])
            self.assertEqual('failure_receipt', row['failure_time_basis'])

    def test_doctor_reports_profiles_and_failed_attempt_without_content(self):
        with tempfile.TemporaryDirectory() as directory:
            project = str(Path(directory) / 'project')
            profile = dict(runtime='pi', provider='fixture', model='fixture', reasoning_effort='low', timeout=5)
            configure(directory, capture_scope='all', project_memory_mode='shadow', project_memory_projects=[project],
                      processing_profile=profile, observation_profile=dict(profile, runtime='codex', provider='openai'))
            with Store(directory) as store:
                store.remember(project, 'Private source', 'Do not expose this source.', source='hook:UserPromptSubmit', session_id='task')
                memory = ProjectMemory(store)
                job = memory.claim(project)
                attempt = memory.begin_attempt(job, model='fixture', reasoning_effort='low', profile=ExecutionProfile(**profile))
                memory.finalize_attempt(project, attempt['attempt_id'], 'failed',
                    {'code': 'runner_failure', 'reason_code': 'authentication_unavailable'})
            output = io.StringIO()
            with patch.dict(os.environ, PATH=''), redirect_stdout(output):
                self.assertEqual(0, main(['--data-dir', directory, 'doctor']))
            execution = json.loads(output.getvalue())['execution']
            self.assertEqual(profile, execution['default_profile'])
            self.assertEqual('observation_profile', execution['processors']['observation']['source'])
            self.assertEqual('missing_executable', execution['processors']['project_memory']['availability']['status'])
            self.assertEqual('authentication_unavailable', execution['latest_failure']['reason_code'])
            self.assertIsNone(execution['latest_failure']['actual_model'])
            self.assertIn('native', execution['latest_failure']['action'])
            self.assertNotIn('Do not expose this source', output.getvalue())

    def test_codex_preflight_checks_auth_and_catalog_without_a_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'codex'
            executable.write_text('#!' + sys.executable + '\n' + (Path(__file__).parent / 'fixtures/execution_codex.py').read_text())
            executable.chmod(0o700)
            for scenario, expected in [({}, None), ({'account': {'account': None, 'requiresOpenaiAuth': True}},
                                                   'authentication_unavailable'), ({'account': {}}, 'unsupported_codex_protocol'),
                                                   ({'missing_method': 'account/read'}, 'unsupported_codex_protocol'),
                                                   ({'missing_method': 'model/list'}, 'unsupported_codex_protocol')]:
                executable.with_suffix('.json').write_text(json.dumps(scenario))
                output = io.StringIO()
                with patch.dict(os.environ, PATH=str(root), HOME=str(root)), redirect_stdout(output):
                    self.assertEqual(0, main(['--data-dir', str(root / 'data'), 'doctor', '--check-execution']))
                self.assertNotIn('PRIVATE_ERROR', output.getvalue())
                result = json.loads(output.getvalue())['execution']['processors']['observation']['availability']
                self.assertEqual(expected, result.get('reason_code'), result)
            self.assertNotIn('turn/start', executable.with_suffix('.trace').read_text())

    def test_explicit_pi_preflight_distinguishes_failures_without_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / 'pi'
            executable.write_text('#!' + sys.executable + '\n' + (Path(__file__).parent / 'fixtures/execution_pi.py').read_text())
            executable.chmod(0o700)
            profile = dict(runtime='pi', provider='fixture', model='fixture', reasoning_effort='low', timeout=5)
            configure(root / 'data', processing_profile=profile)
            cases = [({}, None), ({'auth_status': 'not_ready'}, 'authentication_unavailable'),
                     ({'version': '0.1'}, 'unsupported_pi_version'), ({'models': []}, 'exact_model_unavailable'),
                     ({'levels': ['off']}, 'unsupported_reasoning')]
            for scenario, expected in cases:
                with self.subTest(expected=expected):
                    executable.with_suffix('.json').write_text(json.dumps(scenario))
                    output = io.StringIO()
                    with patch.dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                                    HOME=str(root), PI_CODING_AGENT_DIR=str(root / 'native')), redirect_stdout(output):
                        self.assertEqual(0, main(['--data-dir', str(root / 'data'), 'doctor', '--check-execution']))
                    status = json.loads(output.getvalue())['execution']['processors']['observation']['availability']
                    self.assertEqual(expected, status.get('reason_code'), status)
                    self.assertEqual('checked' if expected is None else 'failed', status['status'])
            trace = executable.with_suffix('.trace').read_text()
            self.assertNotIn('"type": "prompt"', trace)


if __name__ == '__main__':
    unittest.main()
