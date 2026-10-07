"""Dashboard isolation, accounting identity, time bounds and readonly behavior."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from codex_mem.dashboard_data import DashboardReader, _window


class DashboardDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.project = '/projects/alpha'
        self.now = datetime.now(timezone.utc).isoformat()
        self.old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()

    def connection(self):
        connection = sqlite3.connect(self.home / 'memory.sqlite3')
        self.addCleanup(connection.close)
        return connection

    def database(self):
        connection = sqlite3.connect(self.home / 'memory.sqlite3')
        self.addCleanup(connection.close)
        connection.executescript('''
            CREATE TABLE entries(id TEXT,project TEXT,kind TEXT,session_id TEXT,created_at TEXT,updated_at TEXT,body TEXT,title TEXT);
            CREATE TABLE usage_sessions(thread_id TEXT,session_id TEXT,parent_thread_id TEXT,project TEXT,agent_path TEXT);
            CREATE TABLE usage_events(event_key TEXT,thread_id TEXT,session_id TEXT,response_id TEXT,model TEXT,model_source TEXT,service_tier TEXT,service_tier_source TEXT,recorded_at TEXT,source_kind TEXT,input_tokens INT,cached_input_tokens INT,cache_write_input_tokens INT,output_tokens INT,reasoning_output_tokens INT,total_tokens INT,private_payload TEXT);
            CREATE TABLE observation_jobs(id TEXT,project TEXT,session_id TEXT,status TEXT,error_code TEXT,attempt_count INT,model TEXT,updated_at TEXT,created_at TEXT);
            CREATE TABLE observer_usage_attempts(job_id TEXT,attempt_count INT,worker_thread_id TEXT,started_at TEXT,usage_status TEXT,outcome TEXT,input_tokens INT,cached_input_tokens INT,cache_write_input_tokens INT,output_tokens INT,reasoning_output_tokens INT,total_tokens INT);
            CREATE TABLE jev_filter_attempts(job_id TEXT,attempt_count INT,audit_json TEXT,updated_at TEXT);
            CREATE TABLE jev_judgment_audits(id INT,project TEXT,owner_id TEXT,audit_json TEXT,created_at TEXT);
        ''')
        return connection

    def event(self, connection, event='e', thread='main', response='r', recorded=None, model='gpt-6-astra'):
        connection.execute('INSERT INTO usage_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (event, thread, 'session', response, model, 'response', 'standard', 'token_usage_record', recorded or self.now, 'response', 100, 20, 0, 10, 5, 110, 'PRIVATE_SENTINEL'))

    def seed(self):
        with self.database() as connection:
            connection.executemany('INSERT INTO usage_sessions VALUES (?,?,?,?,?)', [('main', 'session', None, self.project, None), ('child', 'session', 'main', self.project, 'child'), ('worker', 'session', None, self.project, None)])
            self.event(connection)
            self.event(connection, 'duplicate')
            self.event(connection, 'child', 'child', 'child-response')
            self.event(connection, 'worker', 'worker', 'worker-response')
            connection.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)', ('note', self.project, 'observation', 'session', self.now, self.now, 'PRIVATE_SENTINEL', 'PRIVATE_TITLE'))
            connection.execute('INSERT INTO observation_jobs VALUES (?,?,?,?,?,?,?,?,?)', ('job', self.project, 'child', 'failed', 'invalid_response', 1, 'gpt-5.6-luna', self.now, self.now))
            connection.execute('INSERT INTO observer_usage_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', ('job', 1, 'worker', self.now, 'reported', 'processed', 200, 0, 0, 20, 10, 220))
            audit = {'model': 'jev-1.0', 'usage_status': 'reported', 'status': 'success', 'usage': {'input_tokens': 7, 'output_tokens': 3}, 'counts': {'requests': 1}, 'cache_key': 'PRIVATE_SENTINEL', 'decisions': ['PRIVATE_SENTINEL']}
            connection.execute('INSERT INTO jev_filter_attempts VALUES (?,?,?,?)', ('job', 1, json.dumps(audit), self.now))
            connection.execute('INSERT INTO jev_judgment_audits VALUES (?,?,?,?,?)', (1, self.project, 'job', json.dumps(dict(audit, route='quality_refinement_commit')), self.now))
            connection.execute('INSERT INTO jev_judgment_audits VALUES (?,?,?,?,?)', (2, self.project, None, json.dumps(dict(audit, route='retrieval_rank_success')), self.now))

    def test_missing_corrupt_do_not_create_files_or_fake_zero_cost(self):
        absent = self.home / 'absent'
        result = DashboardReader(absent).overview()
        self.assertFalse(absent.exists())
        self.assertEqual('unavailable', result['status'])
        self.assertIsNone(result['main']['total_tokens'])
        self.assertIsNone(result['combined']['api_equivalent_usd']['total'])
        self.assertIsNone(result['capture']['entries'])
        self.assertIsNone(result['queue']['failed'])
        self.assertIsNone(result['jev']['filter']['receipts'])
        (self.home / 'memory.sqlite3').write_bytes(b'corrupt')
        before = sorted(self.home.iterdir())
        self.assertEqual('unavailable', DashboardReader(self.home).overview()['status'])
        self.assertEqual(before, sorted(self.home.iterdir()))

    def test_dedup_main_child_and_observer_are_additive_and_isolated(self):
        self.seed()
        reader = DashboardReader(self.home)
        overview = reader.overview()
        self.assertEqual(220, overview['main']['total_tokens'])
        self.assertEqual(220, overview['observer']['total_tokens'])
        self.assertEqual(440, overview['combined']['total_tokens'])
        self.assertEqual(1, overview['completeness']['duplicate_response_rows_removed'])
        self.assertEqual(1, overview['completeness']['observer_overlap_events_excluded_from_main'])
        session = reader.session(self.project, 'session')
        self.assertEqual(440, session['combined']['total_tokens'])
        self.assertEqual('usage_session_mapping', next(iter(session['observer']['session_attribution']['basis_counts'])))
        self.assertEqual(3, len(session['threads']))
        self.assertEqual(1, len(session['processor_attempts']))
        self.assertEqual('gpt-6-astra', session['models']['main'][0]['value'])
        self.assertEqual(220, session['models']['main'][0]['total_tokens'])
        self.assertEqual('gpt-5.6-luna', session['models']['observer'][0]['value'])
        self.assertEqual(220, session['models']['observer'][0]['total_tokens'])
        self.assertEqual(['gpt-6-astra'], session['threads'][0]['models'])
        self.assertEqual('gpt-5.6-luna', session['processor_attempts'][0]['priced_usage']['model'])
        self.assertEqual(220, session['processor_attempts'][0]['priced_usage']['tokens']['total_tokens'])
        project_detail = reader.project(self.project)
        self.assertEqual(220, project_detail['models']['main'][0]['total_tokens'])
        self.assertEqual(3, project_detail['sessions'][0]['thread_count'])
        self.assertNotIn('processor_attempts', project_detail['sessions'][0])
        self.assertEqual(7, session['jev']['filter']['input_tokens'])
        self.assertIsNone(session['jev']['estimated_usd'])
        self.assertEqual(0, session['jev']['retrieval']['receipts'])
        self.assertEqual(0, reader.session('/projects/beta', 'session')['main']['total_tokens'])
        serialized = json.dumps(session)
        self.assertNotIn('PRIVATE', serialized)
        self.assertNotIn('cache_key', serialized)

    def test_readonly_does_not_construct_writers_or_change_database(self):
        self.seed()
        path = self.home / 'memory.sqlite3'
        before = path.read_bytes()
        names = sorted(self.home.iterdir())
        with patch('codex_mem.store.Store', side_effect=AssertionError('writer')), patch('codex_mem.usage_store.UsageStore', side_effect=AssertionError('writer')):
            reader = DashboardReader(self.home)
            json.dumps(reader.overview())
            json.dumps(reader.projects())
            json.dumps(reader.project(self.project))
            json.dumps(reader.session(self.project, 'session'))
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(names, sorted(self.home.iterdir()))

    def test_period_bounds_and_queue_use_distinct_time_bases(self):
        self.seed()
        with self.connection() as connection:
            self.event(connection, 'old', response='old', recorded=self.old)
            connection.execute('UPDATE observation_jobs SET created_at=?,updated_at=?', (self.old, self.old))
        reader = DashboardReader(self.home)
        self.assertEqual(330, reader.overview()['main']['total_tokens'])
        today = reader.overview('today')
        self.assertEqual(220, today['main']['total_tokens'])
        self.assertEqual(1, today['queue']['failed'])
        self.assertEqual(1, today['queue']['quarantined'])
        for value in ('today', '7d', '30d', 'all'):
            window = _window(value)
            self.assertEqual('Asia/Jerusalem', window['timezone'])
            self.assertLess(datetime.fromisoformat(window['from']), datetime.fromisoformat(window['to']))
        with self.assertRaises(ValueError):
            reader.overview('month')

    def test_pagination_search_and_usage_only_project(self):
        self.seed()
        with self.connection() as connection:
            connection.execute('INSERT INTO usage_sessions VALUES (?,?,?,?,?)', ('beta', 'beta', None, '/projects/beta', None))
            connection.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)', ('other', '/projects/gamma', 'note', None, self.now, self.now, '', ''))
        reader = DashboardReader(self.home)
        result = reader.projects(page=2, limit=1)
        self.assertEqual(3, result['pagination']['total'])
        self.assertEqual('/projects/gamma', result['projects'][0]['project'])
        self.assertEqual('/projects/gamma', reader.projects(query='GAMMA')['projects'][0]['project'])
        for kwargs in ({'page': 0}, {'limit': 101}, {'page': True}, {'query': 3}):
            with self.assertRaises(ValueError):
                reader.projects(**kwargs)

    def test_pending_captures_match_processor_source_exclusion_and_large_jev_receipts(self):
        self.seed()
        with self.connection() as connection:
            connection.executescript("ALTER TABLE entries ADD COLUMN source TEXT; ALTER TABLE entries ADD COLUMN superseded_by TEXT; ALTER TABLE observation_jobs ADD COLUMN disposition TEXT; ALTER TABLE observation_jobs ADD COLUMN reasoning_effort TEXT; CREATE TABLE observation_job_sources(job_id TEXT,source_id TEXT);")
            connection.execute("UPDATE entries SET source='hook:Stop'")
            connection.execute("INSERT INTO entries(id,project,kind,session_id,created_at,updated_at,source) VALUES ('fresh',?,'raw','session',?,?,'hook:PostToolUse:x')", (self.project,self.now,self.now))
            connection.execute("INSERT INTO observation_job_sources VALUES ('job','note')")
            large = {'decisions': ['PRIVATE_SENTINEL' * 10000], 'usage': {'input_tokens': 11, 'output_tokens': 4}, 'usage_status': 'reported', 'counts': {'requests': 1}}
            connection.execute('UPDATE jev_filter_attempts SET audit_json=?', (json.dumps(large,sort_keys=True),))
        report = DashboardReader(self.home).overview()
        self.assertEqual(1, report['queue']['pending_observations'])
        self.assertEqual(1, report['queue']['pending'])
        self.assertEqual(11, report['jev']['filter']['input_tokens'])
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(report))

    def test_projects_sessions_and_notes_sort_by_recent_activity(self):
        self.seed()
        with self.connection() as connection:
            connection.execute('INSERT INTO usage_sessions VALUES (?,?,?,?,?)', ('new', 'new-session', None, self.project, None))
            connection.execute('INSERT INTO usage_sessions VALUES (?,?,?,?,?)', ('old', 'old-session', None, '/projects/beta', None))
            connection.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)', ('new-note', self.project, 'note', 'new-session', self.now, self.now, 'PRIVATE_SENTINEL', ''))
            connection.execute('INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)', ('old-note', '/projects/beta', 'note', 'old-session', self.old, self.old, '', ''))
            connection.execute('UPDATE entries SET created_at=? WHERE id=?', (self.old, 'note'))
            connection.execute('UPDATE observation_jobs SET updated_at=?', (self.old,))
            connection.execute('UPDATE usage_events SET recorded_at=?', (self.old,))
        reader = DashboardReader(self.home)
        self.assertEqual(self.project, reader.projects()['projects'][0]['project'])
        project = reader.project(self.project)
        self.assertEqual('new-session', project['sessions'][0]['session_id'])
        self.assertEqual(self.now, project['capture']['last_note_at'])

    def test_unknown_model_preserves_unpriced_amount(self):
        with self.database() as connection:
            connection.execute('INSERT INTO usage_sessions VALUES (?,?,?,?,?)', ('main', 'session', None, self.project, None))
            self.event(connection, model='unknown-model')
        report = DashboardReader(self.home).overview()
        self.assertEqual(110, report['main']['total_tokens'])
        self.assertEqual(1, report['main']['api_equivalent_usd']['unpriced_events'])
        self.assertIsNone(report['main']['api_equivalent_usd']['total'])

    def test_deadline_and_cap_are_reported_without_partial_zero_totals(self):
        self.seed()
        with patch('codex_mem.dashboard_data.ROW_LIMIT', 1):
            report = DashboardReader(self.home).overview()
        self.assertEqual('partial', report['status'])
        self.assertIn('usage_events', report['coverage']['truncated_tables'])
        self.assertIsNone(report['combined']['api_equivalent_usd']['total'])
        with patch('codex_mem.dashboard_data.QUERY_SECONDS', -1):
            report = DashboardReader(self.home).overview()
        self.assertEqual('unavailable', report['status'])

    def test_deadline_retries_before_normal_cache_expiry(self):
        self.seed()
        reader = DashboardReader(self.home)
        with patch('codex_mem.dashboard_data.QUERY_SECONDS', -1):
            failed = reader.overview()
        self.assertEqual('query_deadline', failed['coverage']['status'])
        timestamp, snapshot = reader._failures[('all', None)]
        reader._failures[('all', None)] = (timestamp - 3, snapshot)
        with patch.object(reader, '_read', wraps=reader._read) as read:
            recovered = reader.overview()
        self.assertEqual('available', recovered['status'])
        self.assertEqual(440, recovered['combined']['total_tokens'])
        self.assertEqual(1, read.call_count)

    def test_expired_snapshot_survives_transient_deadline_with_staleness(self):
        self.seed()
        reader = DashboardReader(self.home)
        before = reader.overview()
        cache_key = ('all', None)
        reader._cache[cache_key] = (0, reader._cache[cache_key][1])
        with patch('codex_mem.dashboard_data.QUERY_SECONDS', -1):
            failed = reader.overview()
        self.assertEqual('stale', failed['status'])
        self.assertEqual(before['combined']['total_tokens'], failed['combined']['total_tokens'])
        self.assertTrue(failed['coverage']['stale'])
        self.assertEqual('query_deadline', failed['coverage']['refresh_error'])
        self.assertEqual('stale', failed['completeness']['collection']['status'])
        self.assertGreaterEqual(failed['coverage']['snapshot_age_seconds'], 30)
        self.assertEqual('available', before['coverage']['status'])
        self.assertEqual('available', reader._cache[cache_key][1]['status'])

    def test_missing_and_corrupt_have_distinct_failure_reasons(self):
        self.assertEqual('database_missing', DashboardReader(self.home).overview()['coverage']['status'])
        (self.home / 'memory.sqlite3').write_bytes(b'corrupt')
        self.assertEqual('database_corrupt', DashboardReader(self.home).overview()['coverage']['status'])

    def test_ancillary_query_deadline_preserves_completed_accounting_ledgers(self):
        self.seed()
        connect = sqlite3.connect
        interrupted_table = 'jev_judgment_audits'

        class InterruptJudgments:
            def __init__(self, *args, **kwargs):
                self.connection = connect(*args, **kwargs)

            @property
            def row_factory(self):
                return self.connection.row_factory

            @row_factory.setter
            def row_factory(self, value):
                self.connection.row_factory = value

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def execute(self, query, *args):
                if query.startswith('SELECT ') and f'FROM {interrupted_table}' in query and not query.startswith('SELECT DISTINCT '):
                    raise sqlite3.OperationalError('interrupted')
                return self.connection.execute(query, *args)

        with patch('codex_mem.dashboard_data.sqlite3.connect', InterruptJudgments):
            report = DashboardReader(self.home).overview()
        self.assertEqual('partial', report['status'])
        self.assertEqual('query_deadline', report['coverage']['status'])
        self.assertEqual(440, report['combined']['total_tokens'])
        self.assertEqual(1, report['capture']['entries'])
        self.assertIsNone(report['jev']['quality']['receipts'])
        self.assertNotIn('jev_judgment_audits', report['coverage']['available_tables'])
        self.assertNotIn('PRIVATE', json.dumps(report))
        interrupted_table = 'observer_usage_attempts'
        reader = DashboardReader(self.home)
        with patch('codex_mem.dashboard_data.sqlite3.connect', InterruptJudgments):
            report = reader.overview()
        self.assertEqual('partial', report['status'])
        self.assertEqual(220, report['main']['total_tokens'])
        self.assertIsNone(report['observer']['total_tokens'])
        self.assertIsNone(report['combined']['total_tokens'])
        self.assertEqual(1, report['completeness']['observer_overlap_events_excluded_from_main'])
        timestamp, snapshot = reader._failures[('all', None)]
        reader._failures[('all', None)] = (timestamp - 3, snapshot)
        with patch('codex_mem.dashboard_data.QUERY_SECONDS', -1):
            failed_again = reader.overview()
        self.assertEqual('stale', failed_again['status'])
        self.assertEqual(220, failed_again['main']['total_tokens'])
        self.assertIsNone(failed_again['observer']['total_tokens'])

    def test_slow_project_read_does_not_block_other_snapshot_scopes(self):
        self.seed()
        reader = DashboardReader(self.home)
        entered, release = threading.Event(), threading.Event()
        read = reader._read

        def delayed(period, project=None):
            if project is not None:
                entered.set()
                release.wait(2)
            return read(period, project)

        with patch.object(reader, '_read', side_effect=delayed), ThreadPoolExecutor(max_workers=2) as pool:
            project = pool.submit(reader.project, self.project)
            try:
                self.assertTrue(entered.wait(1))
                report = pool.submit(reader.overview).result(timeout=1)
                self.assertEqual(440, report['combined']['total_tokens'])
            finally:
                release.set()
            self.assertEqual('available', project.result(timeout=1)['status'])


if __name__ == '__main__':
    unittest.main()
