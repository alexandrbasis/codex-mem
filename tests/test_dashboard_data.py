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

    def test_failure_reasons_without_new_sqlite_attributes(self):
        (self.home / 'memory.sqlite3').touch()
        constants = ('SQLITE_INTERRUPT', 'SQLITE_BUSY', 'SQLITE_LOCKED', 'SQLITE_CORRUPT', 'SQLITE_NOTADB')
        cases = (
            ('query deadline exceeded', 'query_deadline'),
            ('interrupted', 'query_deadline'),
            ('database is locked', 'database_busy'),
            ('database table is locked', 'database_busy'),
            ('database disk image is malformed', 'database_corrupt'),
            ('file is not a database', 'database_corrupt'),
            ('private path /secret unreadable', 'database_unreadable'),
        )
        with patch.dict(sqlite3.__dict__):
            for name in constants:
                sqlite3.__dict__.pop(name, None)
            for message, expected in cases:
                with self.subTest(message=message):
                    error = sqlite3.OperationalError(message)
                    self.assertFalse(hasattr(error, 'sqlite_errorcode'))
                    with patch.object(sqlite3, 'connect', side_effect=error):
                        result = DashboardReader(self.home).overview()
                    self.assertEqual(expected, result['coverage']['status'])
                    self.assertIsNone(result['main']['total_tokens'])
                    self.assertIsNone(result['combined']['api_equivalent_usd']['total'])
                    self.assertNotIn('/secret', json.dumps(result))

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

    def test_queue_progress_uses_terminal_jobs_and_snapshot_hour_across_periods(self):
        end = datetime.fromisoformat(self.now)
        recent = (end - timedelta(minutes=10)).isoformat()
        older = (end - timedelta(hours=2)).isoformat()
        later = (end + timedelta(minutes=1)).isoformat()
        with self.database() as connection:
            connection.execute('ALTER TABLE observation_jobs ADD COLUMN completed_at TEXT')
            connection.executemany('INSERT INTO observation_jobs(id,project,status,created_at,updated_at,completed_at) VALUES (?,?,?,?,?,?)', [
                ('processed', self.project, 'processed', self.old, recent, recent),
                ('skipped', self.project, 'skipped', self.old, recent, None),
                ('failed', self.project, 'failed', self.old, self.now, self.now),
                ('old', self.project, 'processed', self.old, self.now, older),
                ('running', self.project, 'running', self.old, self.now, None),
                ('pending', self.project, 'pending', self.old, self.now, None),
                ('future', self.project, 'processed', self.old, later, later),
                ('beta', '/projects/beta', 'skipped', self.old, recent, recent),
            ])
        reader = DashboardReader(self.home)
        for period in ('all', 'today', '7d', '30d'):
            window = dict(_window(period), to=self.now)
            with patch('codex_mem.dashboard_data._window', return_value=window):
                queue = reader.overview(period)['queue']
                scoped = reader.project(self.project, period)['queue']['progress']
            self.assertEqual('current_snapshot_last_hour', scoped['basis'])
            self.assertEqual(3600, scoped['window_seconds'])
            self.assertTrue(scoped['available'])
            self.assertEqual(1, scoped['processed_jobs'])
            self.assertEqual(1, scoped['skipped_jobs'])
            self.assertEqual(1, scoped['failed_jobs'])
            self.assertEqual(recent, scoped['last_completed_at'])
            self.assertEqual(self.now, scoped['last_attempt_at'])
            self.assertEqual(2, queue['progress']['skipped_jobs'])
            self.assertEqual(1, queue['running'])
            self.assertEqual(1, queue['pending_jobs'])

    def test_queue_progress_never_claims_work_from_running_or_pending_updates(self):
        with self.database() as connection:
            connection.executemany('INSERT INTO observation_jobs(id,project,status,updated_at) VALUES (?,?,?,?)', [
                ('running', self.project, 'running', self.now),
                ('pending', self.project, 'pending', self.now),
            ])
        progress = DashboardReader(self.home).overview()['queue']['progress']
        self.assertTrue(progress['available'])
        self.assertEqual(0, progress['processed_jobs'])
        self.assertEqual(0, progress['skipped_jobs'])
        self.assertEqual(0, progress['failed_jobs'])
        self.assertIsNone(progress['last_completed_at'])
        self.assertIsNone(progress['last_attempt_at'])

    def test_queue_progress_requires_complete_jobs_but_preserves_unrelated_partial_reads(self):
        reader = DashboardReader(self.home)
        missing = reader.overview()['queue']['progress']
        self.assertFalse(missing['available'])
        self.assertIsNone(missing['processed_jobs'])
        self.seed()
        with self.connection() as connection:
            connection.execute('INSERT INTO observation_jobs(id,project,status,updated_at) VALUES (?,?,?,?)', ('extra', self.project, 'processed', self.now))
        with patch('codex_mem.dashboard_data.ROW_LIMIT', 1):
            truncated = DashboardReader(self.home).overview()['queue']['progress']
        self.assertFalse(truncated['available'])
        self.assertIsNone(truncated['failed_jobs'])
        snapshot = reader._read('all')
        snapshot['coverage']['interrupted_tables'] = ['observation_jobs']
        self.assertFalse(reader._queue(snapshot)['progress']['available'])
        snapshot['coverage']['interrupted_tables'] = ['jev_judgment_audits']
        snapshot['status'] = 'partial'
        self.assertTrue(reader._queue(snapshot)['progress']['available'])

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
            connection.execute("INSERT INTO entries(id,project,kind,session_id,created_at,updated_at,source) VALUES ('fresh',?,'raw','session',?,?,'hook:PostToolUse:x')", (self.project,self.old,self.now))
            connection.execute("INSERT INTO observation_job_sources VALUES ('job','note')")
            large = {'decisions': ['PRIVATE_SENTINEL' * 10000], 'usage': {'input_tokens': 11, 'output_tokens': 4}, 'usage_status': 'reported', 'counts': {'requests': 1}}
            connection.execute('UPDATE jev_filter_attempts SET audit_json=?', (json.dumps(large,sort_keys=True),))
        report = DashboardReader(self.home).overview()
        self.assertEqual(1, report['queue']['pending_observations'])
        self.assertEqual(1, report['queue']['pending'])
        self.assertEqual(self.old, report['queue']['pending_oldest_at'])
        self.assertEqual(self.old, DashboardReader(self.home).project(self.project, 'today')['queue']['pending_oldest_at'])
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

    def timed_snapshot(self, statement_seconds, query_seconds, snapshot_seconds):
        real_connect = sqlite3.connect
        clock = [0.0]

        class TimedConnection(sqlite3.Connection):
            def set_progress_handler(self, callback, instructions):
                self.check_deadline = callback
                return super().set_progress_handler(callback, instructions)

            def execute(self, query, *args, **kwargs):
                cursor = super().execute(query, *args, **kwargs)
                if query.startswith('SELECT ') and not query.startswith(('SELECT name ', 'SELECT DISTINCT ')):
                    clock[0] += statement_seconds
                    if self.check_deadline():
                        raise sqlite3.OperationalError('interrupted')
                return cursor

        def connect(*args, **kwargs):
            return real_connect(*args, factory=TimedConnection, **kwargs)

        with patch('codex_mem.dashboard_data.sqlite3.connect', side_effect=connect), \
                patch('codex_mem.dashboard_data.time.monotonic', side_effect=lambda: clock[0]), \
                patch('codex_mem.dashboard_data.QUERY_SECONDS', query_seconds), \
                patch('codex_mem.dashboard_data.SNAPSHOT_SECONDS', snapshot_seconds):
            snapshot = DashboardReader(self.home)._read('all')
        return snapshot, clock[0]

    def test_fast_statements_do_not_share_one_query_deadline(self):
        self.seed()
        snapshot, elapsed = self.timed_snapshot(1, 2, 20)
        self.assertGreater(elapsed, 2)
        self.assertEqual('available', snapshot['status'])
        self.assertEqual(7, len(snapshot['coverage']['available_tables']))

    def test_statement_and_whole_snapshot_deadlines_still_stop_slow_reads(self):
        self.seed()
        snapshot, elapsed = self.timed_snapshot(3, 2, 20)
        self.assertEqual('query_deadline', snapshot['coverage']['status'])
        self.assertEqual('unavailable', snapshot['status'])
        self.assertEqual(3, elapsed)
        snapshot, elapsed = self.timed_snapshot(1, 2, 3)
        self.assertEqual('query_deadline', snapshot['coverage']['status'])
        self.assertEqual('partial', snapshot['status'])
        self.assertEqual(3, elapsed)
        self.assertNotIn('observation_jobs', snapshot['coverage']['available_tables'])

    def test_warm_failure_does_not_retain_a_second_full_dataset(self):
        self.seed()
        reader = DashboardReader(self.home)
        before = reader.overview()
        reader._cache[('all', None)] = (0, reader._cache[('all', None)][1])
        failed = reader._read('all')
        failed['status'] = 'partial'
        failed['coverage']['status'] = 'query_deadline'
        with patch.object(reader, '_read', return_value=failed) as read:
            after = reader.overview()
            retried = reader.overview()
        self.assertEqual(1, read.call_count)
        self.assertEqual('stale', retried['status'])
        self.assertEqual('query_deadline', retried['coverage']['refresh_error'])
        self.assertEqual('stale', after['status'])
        self.assertEqual(before['combined']['total_tokens'], after['combined']['total_tokens'])
        self.assertNotIn('tables', reader._failures[('all', None)][1])

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
        self.assertTrue(report['queue']['progress']['available'])
        self.assertEqual(1, report['queue']['progress']['failed_jobs'])
        self.assertIsNone(report['jev']['quality']['receipts'])
        self.assertNotIn('jev_judgment_audits', report['coverage']['available_tables'])
        self.assertNotIn('PRIVATE', json.dumps(report))
        interrupted_table = 'observation_jobs'
        with patch('codex_mem.dashboard_data.sqlite3.connect', InterruptJudgments):
            interrupted = DashboardReader(self.home).overview()['queue']['progress']
        self.assertFalse(interrupted['available'])
        self.assertIsNone(interrupted['failed_jobs'])
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
