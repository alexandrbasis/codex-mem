"""Execution dimensions and uncertainty through real disposable ledgers."""
import tempfile
import unittest
from pathlib import Path

from codex_mem.config import configure
from codex_mem.execution import ExecutionProfile
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store
from codex_mem.usage_api import usage_report


class ExecutionUsageReportTests(unittest.TestCase):
    def test_missing_ledger_is_unknown_but_initialized_empty_ledger_is_zero(self):
        from codex_mem.usage import UsageStore
        with tempfile.TemporaryDirectory() as directory:
            options = dict(from_date='2020-01-01', to_date='2030-01-01', group_by=['runtime'])
            missing = usage_report(directory, **options)
            for stream in ('main', 'observer', 'combined'):
                self.assertIsNone(missing[stream]['token_totals']['total_tokens']['total'])
                self.assertEqual(0, missing[stream]['token_totals']['total_tokens']['known_subtotal'])
            with Store(directory) as store, UsageStore(directory):
                from codex_mem.observer_usage_store import _install_schema
                _install_schema(store._connection)
            empty = usage_report(directory, **options)
            for stream in ('main', 'observer', 'combined'):
                self.assertEqual(0, empty[stream]['token_totals']['total_tokens']['total'])

    def test_requested_pi_failure_keeps_measured_fields_and_its_execution_group(self):
        with tempfile.TemporaryDirectory() as directory:
            project = str((Path(directory) / 'project').resolve())
            configure(directory, capture_scope='all', project_memory_mode='shadow', project_memory_projects=[project])
            with Store(directory) as store:
                store.remember(project, 'Prompt', 'Keep deployment open.', source='hook:UserPromptSubmit', session_id='task')
                memory = ProjectMemory(store)
                claim = memory.claim(project)
                profile = ExecutionProfile(runtime='pi', provider='anthropic', model='gpt-6-luna', reasoning_effort='medium', timeout=120)
                first = memory.begin_attempt(claim, model=profile.model, reasoning_effort='medium', profile=profile)
                memory.finalize_attempt(project, first['attempt_id'], 'failed', {
                    'usage_status': 'reported', 'usage_source': 'pi_assistant_usage',
                    'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120, 'code': 'timeout'})
            report = usage_report(directory, project=project, from_date='2020-01-01', to_date='2030-01-01',
                                  group_by=['runtime', 'provider', 'execution', 'model'])
            row = report['groups']['observer']['execution']['rows'][0]
            self.assertEqual({'runtime': 'pi', 'provider': 'anthropic', 'model': 'gpt-6-luna', 'reasoning_effort': 'medium'}, row['execution'])
            self.assertEqual({'timeout': 1}, row['error_code_counts'])
            self.assertEqual({'failed': 1}, row['outcome_counts'])
            self.assertEqual({'attempt_profile': 1}, row['model_source_counts'])
            self.assertEqual(100, row['token_totals']['input_tokens']['total'])
            self.assertIsNone(row['token_totals']['cached_input_tokens']['total'])
            self.assertEqual(0, row['token_totals']['cached_input_tokens']['known_subtotal'])
            self.assertEqual('0', row['api_equivalent_usd']['standard_scenario_subtotal'])
            self.assertIsNone(row['estimated_codex_credits']['total'])

    def test_openai_pi_estimate_never_includes_codex_credits_and_other_provider_is_unpriced(self):
        with tempfile.TemporaryDirectory() as directory:
            project = str((Path(directory) / 'project').resolve())
            configure(directory, capture_scope='all', project_memory_mode='shadow', project_memory_projects=[project])
            with Store(directory) as store:
                store.remember(project, 'Prompt', 'Keep the feature open.', source='hook:UserPromptSubmit', session_id='task')
                memory = ProjectMemory(store)
                claim = memory.claim(project)
                for provider in ('openai', 'anthropic'):
                    profile = ExecutionProfile(runtime='pi', provider=provider, model='gpt-6-luna', reasoning_effort='medium', timeout=120)
                    attempt = memory.begin_attempt(claim, model=profile.model, reasoning_effort='medium', profile=profile)
                    receipt = dict(usage_status='reported', input_tokens=100, cached_input_tokens=0,
                                   cache_write_input_tokens=0, output_tokens=20, reasoning_output_tokens=0, total_tokens=120,
                                   identity={key: getattr(profile, key) for key in ('runtime', 'provider', 'model', 'reasoning_effort')})
                    memory.checkpoint_attempt(project, attempt['attempt_id'], receipt)
                    memory.finalize_attempt(project, attempt['attempt_id'], 'processed', receipt)
            report = usage_report(directory, project=project, from_date='2020-01-01', to_date='2030-01-01', group_by=['provider'])
            rows = {row['value']: row for row in report['groups']['observer']['provider']['rows']}
            self.assertEqual('0.00002', rows['openai']['api_equivalent_usd']['standard_scenario_subtotal'])
            self.assertEqual('0', rows['anthropic']['api_equivalent_usd']['standard_scenario_subtotal'])
            for row in rows.values():
                self.assertIsNone(row['estimated_codex_credits']['total'])
                self.assertEqual({'runtime_verified': 1}, row['model_source_counts'])
            self.assertEqual(200, report['observer']['token_totals']['input_tokens']['total'])
            self.assertEqual(0, report['completeness']['duplicate_attempt_rows_removed'])

    def test_observer_status_groups_verified_attempt_identity(self):
        from codex_mem.observer_usage_store import finish_attempt
        with tempfile.TemporaryDirectory() as directory:
            project = str((Path(directory) / 'project').resolve())
            configure(directory, capture_scope='all')
            profile = dict(runtime='pi', provider='anthropic', model='fixture-exact', reasoning_effort='low', timeout=120)
            with Store(directory) as store:
                store.remember(project, 'Prompt', 'Keep the feature open.', source='hook:UserPromptSubmit', session_id='task')
                claim = store.claim_observation_batch(project, 'observer-fixture', 'fixture-exact', 'low', execution_profile=profile)
                finish_attempt(store, project, claim['job_id'], 1, outcome='failed', error_code='timeout',
                               identity={key: profile[key] for key in ('runtime', 'provider', 'model', 'reasoning_effort')})
                row = store.status(project)['observer_usage']['models'][0]
                self.assertEqual('fixture-exact', row['model'])
                self.assertEqual('runtime_verified', row['model_basis'])
                self.assertEqual('pi', row['runtime'])
                self.assertEqual({'timeout': 1}, row['error_code_counts'])

    def test_migrated_historical_and_new_receipts_share_read_only_report(self):
        import sqlite3
        from tests.test_project_memory_cost_report import attempt
        with tempfile.TemporaryDirectory() as directory:
            project = str((Path(directory) / 'project').resolve())
            path = Path(directory) / 'memory.sqlite3'
            with Store(directory):
                pass
            with sqlite3.connect(path) as connection:
                for column in ('requested_runtime', 'requested_provider', 'requested_model', 'requested_reasoning_effort', 'requested_timeout', 'runtime', 'provider', 'actual_model', 'actual_reasoning_effort', 'profile_basis', 'execution_receipt', 'reason_code'):
                    connection.execute('ALTER TABLE pm_usage_attempts DROP COLUMN ' + column)
                historical = attempt(project=project, started_at='2026-10-23T00:00:00Z', updated_at='2026-10-23T00:00:10Z', finished_at='2026-10-23T00:00:10Z')
                connection.execute('INSERT INTO pm_usage_attempts (' + ','.join(historical) + ') VALUES (' + ','.join('?' for _ in historical) + ')', tuple(historical.values()))
            configure(directory, capture_scope='all', project_memory_mode='shadow', project_memory_projects=[project])
            with Store(directory) as store:
                store.remember(project, 'Prompt', 'Keep migration open.', source='hook:UserPromptSubmit', session_id='task')
                memory = ProjectMemory(store)
                claim = memory.claim(project)
                profile = ExecutionProfile(runtime='pi', provider='anthropic', model='fixture-exact', reasoning_effort='low', timeout=120)
                current = memory.begin_attempt(claim, model=profile.model, reasoning_effort='low', profile=profile)
                memory.finalize_attempt(project, current['attempt_id'], 'failed', {'code': 'timeout'})
            before = path.read_bytes()
            report = usage_report(directory, project=project, from_date='2020-01-01', to_date='2030-01-01', group_by=['runtime'])
            self.assertEqual(2, report['observer']['event_count'])
            self.assertEqual({'codex': 1, 'pi': 1}, report['observer']['runtime_counts'])
            self.assertEqual({'legacy_requested_profile': 1, 'attempt_profile': 1}, report['observer']['model_source_counts'])
            self.assertEqual(1000, report['observer']['token_totals']['input_tokens']['known_subtotal'])
            self.assertIsNone(report['observer']['token_totals']['input_tokens']['total'])
            self.assertEqual(before, path.read_bytes())
