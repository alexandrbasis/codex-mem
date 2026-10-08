"""Dashboard query plans must read metadata without fetching entry bodies."""

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from codex_mem.dashboard_data import DashboardReader
from codex_mem.store import SCHEMA_VERSION, Store


METADATA_INDEX = 'entries_dashboard_metadata_idx'
PENDING_INDEX = 'entries_dashboard_pending_idx'


class DashboardIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        with Store(self.home) as store:
            self.path = store.db_path

    def connection(self):
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        return connection

    def test_new_and_existing_stores_install_only_two_additive_indexes(self):
        connection = self.connection()
        indexes = {row[1] for row in connection.execute('PRAGMA index_list(entries)')}
        self.assertTrue({METADATA_INDEX, PENDING_INDEX} <= indexes)
        connection.execute(f'DROP INDEX {METADATA_INDEX}')
        connection.execute(f'DROP INDEX {PENDING_INDEX}')
        connection.commit()
        before_version = connection.execute('PRAGMA user_version').fetchone()[0]
        for _ in range(2):
            with Store(self.home):
                pass
        self.assertEqual(SCHEMA_VERSION, before_version)
        self.assertEqual(before_version, connection.execute('PRAGMA user_version').fetchone()[0])
        self.assertEqual(indexes, {row[1] for row in connection.execute('PRAGMA index_list(entries)')})
        self.assertEqual(
            ['project', 'created_at', 'id', 'kind', 'session_id', 'updated_at', 'superseded_by', 'source'],
            [row[2] for row in connection.execute(f'PRAGMA index_info({METADATA_INDEX})')],
        )
        self.assertEqual(
            ['project', 'source', 'id', 'created_at', 'superseded_by'],
            [row[2] for row in connection.execute(f'PRAGMA index_info({PENDING_INDEX})')],
        )
        pending_definition = connection.execute(
            'SELECT sql FROM sqlite_master WHERE name=?', (PENDING_INDEX,)
        ).fetchone()[0]
        self.assertIn('WHERE superseded_by IS NULL', pending_definition)

    def populate(self, connection):
        now = '2026-10-08T10:00:00Z'
        sources = ('hook:Stop', 'hook:UserPromptSubmit', 'hook:PostToolUse:tool', 'processor:test')
        connection.executemany(
            'INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            [(
                f'entry-{index:04d}', f'/projects/{index % 2}', f'Capture {index}',
                ('raw private payload ' * 512) + str(index), 'evidence', f'session-{index % 5}',
                f'turn-{index}', sources[index % len(sources)], '["capture"]', f'dedupe-{index}',
                now, now, 'entry-0000' if index % 7 == 1 else None,
                now if index % 7 == 1 else None,
            ) for index in range(512)],
        )
        connection.executemany(
            'INSERT INTO observation_jobs '
            '(id,project,processor_id,model,reasoning_effort,session_id,input_fingerprint,input_limit,'
            'status,attempt_count,output_ids_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            [(f'job-{index}', f'/projects/{index}', 'processor', 'test-model', 'low', 'session',
              f'fingerprint-{index}', 1000, 'processed', 1, '[]', now, now) for index in range(2)],
        )
        connection.executemany(
            'INSERT INTO observation_job_sources VALUES (?,?)',
            [('job-0', 'entry-0000'), ('job-1', 'entry-0005')],
        )
        connection.commit()
        connection.execute('ANALYZE')

    def dashboard_queries(self, project):
        statements = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with patch('codex_mem.dashboard_data.sqlite3.connect', side_effect=traced_connect):
            snapshot = DashboardReader(self.home)._read('all', project)
        self.assertEqual('available', snapshot['status'])
        metadata = next(sql for sql in statements if sql.startswith('SELECT id,project,kind'))
        pending = next(sql for sql in statements if sql.startswith('SELECT e.project,COUNT(*)'))
        return metadata, pending

    def assert_no_entry_table_fetch(self, connection, sql):
        root_page = connection.execute(
            "SELECT rootpage FROM sqlite_master WHERE type='table' AND name='entries'"
        ).fetchone()[0]
        bytecode = connection.execute('EXPLAIN ' + sql).fetchall()
        table_cursors = {row[2] for row in bytecode if row[1] == 'OpenRead' and row[3] == root_page}
        # SQLite may open a partial-index table cursor and emit DeferredSeek.
        # It never performs that seek unless a later opcode fetches table data.
        table_fetches = [row for row in bytecode if row[1] in ('Column', 'Rowid', 'SeekRowid')
                         and row[2] in table_cursors]
        self.assertEqual([], table_fetches, sql)

    def test_populated_dashboard_queries_cover_all_and_project_scopes(self):
        connection = self.connection()
        self.populate(connection)
        for project in (None, '/projects/0'):
            with self.subTest(project=project):
                metadata, pending = self.dashboard_queries(project)
                metadata_plan = [row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + metadata)]
                pending_plan = [row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + pending)]
                self.assertTrue(any(f'USING COVERING INDEX {METADATA_INDEX}' in detail
                                    for detail in metadata_plan), metadata_plan)
                self.assertTrue(any(f'USING COVERING INDEX {PENDING_INDEX}' in detail
                                    for detail in pending_plan), pending_plan)
                if project is not None:
                    self.assertTrue(any('SEARCH entries' in detail and 'project=?' in detail
                                        for detail in metadata_plan), metadata_plan)
                    self.assertTrue(any('SEARCH e' in detail and 'project=?' in detail
                                        for detail in pending_plan), pending_plan)
                for sql in (metadata, pending):
                    self.assert_no_entry_table_fetch(connection, sql)
                self.assertIn('CROSS JOIN observation_jobs', pending)


if __name__ == '__main__':
    unittest.main()
