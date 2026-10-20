"""PM attempt accounting joins, retry identity, and native response overlap."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_mem.cost_report import build_cost_report, build_report
from codex_mem.config import configure
from codex_mem.project_memory import ProjectMemory
from codex_mem.store import Store
from tests.test_cost_report import observer, response, PERIOD


def attempt(identifier='attempt-1', **changes):
    row = dict(attempt_id=identifier, project='/project', session_id='task', job_id='pmj_same',
               claim_token=identifier, processor_id='project-memory', model='gpt-6-luna', reasoning_effort='medium',
               started_at='2026-09-11T22:00:00Z', updated_at='2026-09-11T22:00:10Z',
               finished_at='2026-09-11T22:00:10Z', outcome='processed', error_code=None,
               worker_thread_id='worker-' + identifier, worker_turn_id='turn', duration_ms=1000,
               usage_status='reported', usage_source='app_server_thread_total', usage_updates=1,
               input_tokens=1000, cached_input_tokens=800, cache_write_tokens=0,
               output_tokens=100, reasoning_tokens=50, total_tokens=1100)
    row.update(changes)
    return row


def write_attempts(connection, values):
    columns = attempt()
    connection.execute('CREATE TABLE pm_usage_attempts (' + ','.join(
        key + (' INTEGER' if isinstance(value, int) else ' TEXT') for key, value in columns.items()) + ')')
    for row in values:
        connection.execute('INSERT INTO pm_usage_attempts VALUES (' + ','.join('?' for _ in columns) + ')',
                           [row[key] for key in columns])


class ProjectMemoryCostReportTests(unittest.TestCase):
    def test_real_ledger_retry_and_source_deletion_remain_reportable(self):
        with tempfile.TemporaryDirectory() as directory:
            project = str((Path(directory) / 'project').resolve())
            configure(directory, capture_scope='all', project_memory_mode='shadow', project_memory_projects=[project])
            with Store(directory) as store:
                source = store.remember(project, 'Prompt', 'Keep deployment open.',
                                        source='hook:UserPromptSubmit', session_id='task')
                memory = ProjectMemory(store)
                original = memory.claim(project)
                metrics = {'usage_status': 'reported', 'input_tokens': 1000, 'output_tokens': 100,
                           'cached_input_tokens': 800, 'cache_write_input_tokens': 0,
                           'reasoning_output_tokens': 50, 'total_tokens': 1100}
                first = memory.begin_attempt(original, model='gpt-6-luna', reasoning_effort='medium')
                memory.finalize_attempt(project, first['attempt_id'], 'failed', {**metrics, 'code': 'timeout'})
                memory.fail(original, 'timeout')
                memory.retry_failed(project)
                retry = memory.claim(project)
                self.assertEqual(original['job_id'], retry['job_id'])
                second = memory.begin_attempt(retry, model='gpt-6-luna', reasoning_effort='medium')
                memory.finalize_attempt(project, second['attempt_id'], 'processed', metrics)
                self.assertNotEqual(first['attempt_id'], second['attempt_id'])
                store.forget(project, [source['id']])
            report = build_cost_report(directory, project=project, from_date='2020-01-01', to_date='2030-01-01')
            self.assertEqual(2, report['observer']['event_count'])
            self.assertEqual(2000, report['observer']['input_tokens'])
            self.assertEqual({'project_memory_processor': 2}, report['observer']['source_kind_counts'])

    def test_failed_and_successful_attempts_same_job_both_count_after_counter_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'memory.sqlite3'
            with sqlite3.connect(path) as connection:
                write_attempts(connection, [attempt('first', outcome='failed', error_code='timeout'), attempt('second')])
            before = path.read_bytes()
            result = build_cost_report(directory, project='/project', **PERIOD)
            self.assertEqual(2, result['observer']['event_count'])
            self.assertEqual(2000, result['observer']['input_tokens'])
            self.assertEqual({'project_memory_processor': 2}, result['observer']['source_kind_counts'])
            self.assertEqual(2, result['completeness']['collection']['project_memory_attempts_in_scope_all_dates'])
            # No provider tier is known. Only hypothetical rate scenarios exist.
            self.assertIsNone(result['observer']['api_equivalent_usd']['total'])
            self.assertGreater(float(result['observer']['api_equivalent_usd']['standard_scenario_subtotal']), 0)
            self.assertEqual(before, path.read_bytes())

    def test_snapshot_duplicates_keep_latest_but_distinct_claim_tokens_both_count(self):
        first = observer(attempt_id='first', source_kind='project_memory_processor', attempt_count=1)
        second = dict(first, usage_updates=2, input_tokens=2000, total_tokens=2100)
        reset = dict(first, attempt_id='reset')
        report = build_report([], [first, second, reset], **PERIOD)
        self.assertEqual(2, report['observer']['event_count'])
        self.assertEqual(3000, report['observer']['input_tokens'])
        self.assertEqual(1, report['completeness']['duplicate_attempt_rows_removed'])

    def test_worker_response_overlap_excluded_even_outside_attempt_period(self):
        with tempfile.TemporaryDirectory() as directory:
            with sqlite3.connect(Path(directory) / 'memory.sqlite3') as connection:
                write_attempts(connection, [attempt(started_at='2026-09-10T20:00:00Z')])
                connection.execute('CREATE TABLE usage_sessions(thread_id TEXT,project TEXT,parent_thread_id TEXT,agent_path TEXT,agent_role TEXT,agent_nickname TEXT)')
                connection.execute("INSERT INTO usage_sessions VALUES('worker-attempt-1','/project',NULL,NULL,NULL,NULL)")
                event = response(thread_id='worker-attempt-1')
                connection.execute('CREATE TABLE usage_events (' + ','.join(
                    key + (' INTEGER' if isinstance(value, int) else ' TEXT') for key, value in event.items()) + ')')
                connection.execute('INSERT INTO usage_events VALUES (' + ','.join('?' for _ in event) + ')', tuple(event.values()))
            report = build_cost_report(directory, project='/project', **PERIOD)
            self.assertEqual(0, report['main']['event_count'])
            self.assertEqual(0, report['observer']['event_count'])
            self.assertEqual(1, report['completeness']['observer_overlap_events_excluded_from_main'])

    def test_project_period_and_root_session_mapping_are_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            with sqlite3.connect(Path(directory) / 'memory.sqlite3') as connection:
                write_attempts(connection, [attempt(session_id='child'), attempt('foreign', project='/other', session_id='child'),
                                           attempt('outside', started_at='2026-09-14T00:00:00Z')])
                connection.execute('CREATE TABLE usage_sessions(thread_id TEXT,session_id TEXT,project TEXT)')
                connection.execute("INSERT INTO usage_sessions VALUES('child','root','/project')")
            report = build_cost_report(directory, project='/project', session_id='root', **PERIOD)
            self.assertEqual(1, report['observer']['event_count'])
            self.assertEqual({'usage_session_mapping': 1}, report['observer']['session_attribution']['basis_counts'])
            self.assertEqual(['child'], report['observer']['session_attribution']['source_session_ids'])
            self.assertEqual(0, build_cost_report(directory, project='/other', session_id='root', **PERIOD)['observer']['event_count'])

    def test_unknown_and_partial_usage_keep_unknown_total(self):
        for status in ('unknown', 'partial'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                with sqlite3.connect(Path(directory) / 'memory.sqlite3') as connection:
                    write_attempts(connection, [attempt(usage_status=status, outcome='failed')])
                report = build_cost_report(directory, **PERIOD)
                self.assertIsNone(report['observer']['api_equivalent_usd']['total'])
                if status == 'unknown':
                    self.assertEqual(1, report['observer']['completeness']['invalid_or_unavailable_usage_events'])
                    self.assertEqual('0', report['observer']['api_equivalent_usd']['standard_scenario_subtotal'])
                else:
                    self.assertEqual(1, report['observer']['completeness']['partial_usage_events'])
                    self.assertGreater(float(report['observer']['api_equivalent_usd']['standard_scenario_subtotal']), 0)

    def test_missing_or_empty_pm_ledger_preserves_legacy_unknown_observer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'memory.sqlite3'
            with sqlite3.connect(path) as connection:
                connection.execute('CREATE TABLE legacy(value TEXT)')
            original = build_cost_report(directory, **PERIOD)
            self.assertFalse(original['completeness']['collection']['project_memory_ledger_available'])
            with sqlite3.connect(path) as connection:
                write_attempts(connection, [])
            current = build_cost_report(directory, **PERIOD)
            self.assertEqual(0, current['observer']['event_count'])
            self.assertIsNone(current['observer']['api_equivalent_usd']['total'])
            self.assertEqual(original['observer']['api_equivalent_usd'], current['observer']['api_equivalent_usd'])
