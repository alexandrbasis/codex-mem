"""Accounting regression tests use synthetic rollouts, never the user's DB."""
import json
import os
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_mem.usage import COLLECTION_BYTE_BUDGET, COLLECTION_FILE_LIMIT, DISCOVERY_ENTRY_BUDGET, PARSER_VERSION, SCAN_FINGERPRINT_BYTE_BUDGET, UsageCollector, coverage_period


class UsageCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / 'codex'
        self.path = self.home / 'sessions' / 'rollout.jsonl'
        self.path.parent.mkdir(parents=True)
        self.collector = UsageCollector(Path(self.tmp.name) / 'memory', self.home)
        self.config = {'capture_enabled': True, 'capture_scope': 'all'}

    def tearDown(self):
        self.collector.close()
        self.tmp.cleanup()

    def write(self, *records, mode='a'):
        with self.path.open(mode) as out:
            for kind, payload in records:
                out.write(json.dumps({'type': kind, 'payload': payload}) + '\n')

    def meta(self, **kwargs):
        return ('session_meta', {'id': 'thread-a', 'cwd': '/project', 'model_provider': 'openai', **kwargs})

    def turn(self, turn='turn-a', model='model-a'):
        return ('turn_context', {'turn_id': turn, 'model': model})

    def native(self, response='response-a', thread='thread-a', turn='turn-a'):
        return ('event_msg', {'type': 'token_usage_record', 'thread_id': thread, 'turn_id': turn, 'response_id': response,
                              'usage': {'input_tokens': 100, 'cached_input_tokens': 30, 'output_tokens': 20, 'reasoning_output_tokens': 5, 'total_tokens': 120}})

    def scan(self, **kwargs):
        return self.collector.scan_file(self.path, config=self.config, **kwargs)

    def rows(self):
        return [dict(r) for r in self.collector.store.store._connection.execute('SELECT * FROM usage_events ORDER BY rowid')]

    def test_partial_tail_and_replay(self):
        self.write(self.meta(), self.turn(), self.native())
        self.assertEqual(self.scan()['events'], 1)
        with self.path.open('ab') as out:
            out.write(b'{"type":"event_msg"')
        offset = self.scan()['offset']
        self.assertLess(offset, self.path.stat().st_size)
        self.write(self.meta(), self.turn(), self.native(), mode='w')
        self.scan()
        self.assertEqual(len(self.rows()), 1)

    def test_model_changes_and_unknown_not_guessed(self):
        self.write(self.meta(), self.turn(), self.native(), self.turn('turn-b', 'model-b'), self.native('response-b', turn='turn-b'), self.native('response-c', turn='missing'))
        self.scan()
        self.assertEqual([r['model'] for r in self.rows()], ['model-a', 'model-b', None])

    def test_nested_and_top_level_settings_keep_request_separate_from_response(self):
        # Native shape observed in local logs, with unrelated settings removed.
        self.write(self.meta(), self.turn(),
                   ('event_msg', {'type': 'thread_settings_applied', 'thread_id': 'thread-a',
                                  'thread_settings': {'service_tier': 'priority', 'model': 'model-a'}}),
                   self.native())
        kind, response = self.native('response-b')
        response['service_tier'] = 'default'
        self.write(('event_msg', {'type': 'thread_settings_applied', 'thread_id': 'foreign',
                                 'thread_settings': {'service_tier': 'flex'}}), (kind, response),
                   ('event_msg', {'type': 'thread_settings_applied', 'thread_id': 'thread-a', 'service_tier': 'default'}),
                   self.native('response-c'))
        self.scan()
        a, b, c = self.rows()
        self.assertIsNone(a['service_tier'])
        self.assertEqual('priority', a['requested_service_tier'])
        self.assertEqual('thread_settings_nested', a['requested_service_tier_source'])
        self.assertEqual('priority', b['requested_service_tier'])
        self.assertEqual('default', b['service_tier'])
        self.assertEqual('token_usage_record', b['service_tier_source'])
        self.assertEqual('default', c['requested_service_tier'])
        self.assertEqual('thread_settings', c['requested_service_tier_source'])

    def test_explicit_null_tier_clears_requested_setting_on_next_response(self):
        self.write(self.meta(), self.turn(),
                   ('event_msg', {'type': 'thread_settings_applied', 'thread_settings': {'service_tier': 'priority'}}), self.native(),
                   ('event_msg', {'type': 'thread_settings_applied', 'thread_settings': {'service_tier': None}}), self.native('response-b'))
        self.scan()
        self.assertEqual(['priority', None], [r['requested_service_tier'] for r in self.rows()])

    def test_parser_version_replays_missing_tier_without_recounting(self):
        self.write(self.meta(), self.turn(),
                   ('event_msg', {'type': 'thread_settings_applied', 'thread_settings': {'service_tier': 'priority'}}), self.native())
        self.scan()
        conn = self.collector.store.store._connection
        checkpoint = self.collector.store.get_checkpoint(self.path)
        state = checkpoint['parser_state']
        state.pop('parser_version')
        conn.execute('UPDATE usage_scan_files SET parser_state=?', (json.dumps(state),))
        conn.execute('UPDATE usage_events SET requested_service_tier=NULL,requested_service_tier_source=NULL')
        self.scan(max_bytes=400)
        self.scan(max_bytes=400)
        self.scan(max_bytes=400)
        self.assertEqual(1, len(self.rows()))
        self.assertEqual(120, self.rows()[0]['total_tokens'])
        self.assertEqual('priority', self.rows()[0]['requested_service_tier'])
        self.assertEqual(PARSER_VERSION, self.collector.store.get_checkpoint(self.path)['parser_state']['parser_version'])

    def test_partial_counters_survive_root_conflict_and_repair(self):
        kind, response = self.native()
        response['session_id'] = 'conflicting-root'
        response['usage'] = {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}
        self.write(self.meta(session_id='explicit-root'), self.turn(), (kind, response))
        self.scan()
        self.assertEqual('root_metadata_conflict_partial_counters', self.rows()[0]['quality'])
        conn = self.collector.store.store._connection
        checkpoint = self.collector.store.get_checkpoint(self.path)
        checkpoint['parser_state'].pop('parser_version')
        conn.execute('UPDATE usage_scan_files SET parser_state=?', (json.dumps(checkpoint['parser_state']),))
        conn.execute("UPDATE usage_events SET quality='response_exact'")
        self.scan()
        self.assertIn('partial_counters', self.rows()[0]['quality'])
        self.assertEqual(120, self.rows()[0]['total_tokens'])

    def test_foreign_inherited_events_rejected(self):
        self.write(self.meta(session_id='root', parent_thread_id='parent', agent_role='worker'),
                   ('session_meta', {'id': 'parent', 'cwd': '/other'}), self.turn(),
                   self.native('parent-response', thread='parent'), self.native())
        self.scan()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['session_id'], 'root')
        session = self.collector.store.store._connection.execute('SELECT * FROM usage_sessions').fetchone()
        self.assertEqual(session['project'], '/project')
        self.assertEqual(session['agent_role'], 'worker')

    def test_legacy_delta_duplicate_and_native_supersession(self):
        def legacy(i, o):
            return ('event_msg', {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': i, 'output_tokens': o, 'total_tokens': i + o}}})
        self.write(self.meta(), self.turn(), legacy(100, 20), legacy(100, 20), legacy(80, 10), legacy(150, 30))
        self.scan()
        self.assertEqual(sum(r['input_tokens'] for r in self.rows()), 150)
        self.write(self.native())
        self.scan()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['source_kind'], 'response')

    def test_fork_legacy_never_bills_copied_baseline(self):
        self.write(self.meta(parent_thread_id='parent'), self.turn(), ('event_msg', {'type': 'token_count', 'info': {'total_token_usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}}}))
        self.scan()
        self.assertEqual(self.rows(), [])

    def test_exclusion_and_private_text_not_persisted(self):
        self.write(self.meta(), ('response_item', {'text': 'VERY_PRIVATE_PROMPT'}), self.turn(), self.native())
        self.config['excluded_projects'] = ['/project']
        self.assertEqual(self.scan()['status'], 'excluded')
        self.assertEqual(self.rows(), [])
        self.config['excluded_projects'] = []
        self.scan()
        checkpoint = self.collector.store.get_checkpoint(self.path)
        self.assertNotIn('VERY_PRIVATE_PROMPT', json.dumps(checkpoint))

    def test_archived_move_and_rotation_deduplicate(self):
        self.write(self.meta(), self.turn(), self.native())
        self.scan()
        archive = self.home / 'archived_sessions'
        archive.mkdir()
        self.path.rename(archive / self.path.name)
        self.collector.collect(config=self.config)
        self.write(self.meta(), self.turn(), self.native(), self.native('new'))
        self.scan()
        self.assertEqual(len(self.rows()), 2)

    def test_symlink_and_filename_owner_rejected(self):
        self.write(self.meta(), self.native())
        link = self.path.parent / 'link.jsonl'
        link.symlink_to(self.path)
        self.assertEqual(self.collector.scan_file(link, config=self.config)['status'], 'outside_scope')
        named = self.path.parent / 'rollout-12345678-1234-1234-1234-123456789abc.jsonl'
        self.path.rename(named)
        self.assertEqual(self.collector.scan_file(named, config=self.config)['status'], 'unsupported')

    def test_budget_boundary_does_not_drop_usage_line(self):
        self.write(self.meta(), self.turn(), self.native())
        for _ in range(5):
            self.scan(max_bytes=400)
        self.assertEqual(len(self.rows()), 1)

    def test_native_payload_supplies_root_when_header_omits_it(self):
        kind, native = self.native()
        native['session_id'] = 'root-task'
        self.write(self.meta(), self.turn(), (kind, native))
        self.scan()
        self.assertEqual(self.rows()[0]['session_id'], 'root-task')

    def test_nested_spawn_and_guardian_metadata(self):
        self.write(self.meta(source={'subagent': {'thread_spawn': {'parent_thread_id': 'root', 'agent_role': 'guardian'}}}), self.turn(), self.native())
        self.scan()
        session = self.collector.store.store._connection.execute('SELECT * FROM usage_sessions').fetchone()
        self.assertEqual(session['parent_thread_id'], 'root')
        self.assertEqual(session['agent_role'], 'guardian')

    def test_round_robin_scans_all_files(self):
        for n in range(3):
            self.path = self.path.parent / f'{n}.jsonl'
            self.write(self.meta(id=f'thread-{n}'), self.native(thread=f'thread-{n}'))
        for _ in range(3):
            self.collector.collect(config=self.config, max_files=1)
        self.assertEqual(len(self.rows()), 3)

    def populate(self, name, thread, modified_at, padding_bytes=0, project='/project'):
        self.path = self.home / 'sessions' / name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.write(self.meta(id=thread, cwd=project), self.turn(), self.native(thread=thread), mode='w')
        if padding_bytes:
            padding = json.dumps({'type': 'response_item', 'payload': {'text': 'x' * 8192}}) + '\n'
            with self.path.open('a') as out:
                for _ in range((padding_bytes + len(padding) - 1) // len(padding)):
                    out.write(padding)
        os.utime(self.path, (modified_at, modified_at))
        return self.path

    def test_recent_sources_do_not_wait_for_historical_sweep_and_replay_is_idempotent(self):
        for n in range(40):
            self.populate(f'{n:03d}-old.jsonl', f'old-{n}', 1_500_000_000 + n)
        self.collector.collect(config=self.config, max_files=4)
        active = self.populate('zzz-active.jsonl', 'active', 2_000_000_000)

        result = self.collector.collect(config=self.config, max_files=4)

        self.assertIn('active', {row['thread_id'] for row in self.rows()})
        self.assertGreater(result['recent_files'], 0)
        self.assertGreater(result['historical_files'], 0)
        self.assertLessEqual(result['files'], 4)
        # An active file that reached EOF is watched between discovery passes.
        self.path = active
        self.write(self.native('new-response', thread='active'))
        os.utime(active, (2_000_000_001, 2_000_000_001))
        self.collector._discovery = iter([None] * (DISCOVERY_ENTRY_BUDGET * 2))
        self.collector.collect(config=self.config, max_files=4)
        self.assertEqual(2, sum(row['thread_id'] == 'active' for row in self.rows()))
        for _ in range(24):
            self.collector.collect(config=self.config, max_files=4)
        self.assertEqual(42, len(self.rows()))
        self.assertEqual(42 * 120, sum(row['total_tokens'] for row in self.rows()))

    def test_large_active_file_preserves_historical_file_and_byte_budget(self):
        old = self.populate('000-old.jsonl', 'old', 1_500_000_000, padding_bytes=12 * 1024 * 1024)
        active = self.populate('zzz-active.jsonl', 'active', 2_000_000_000, padding_bytes=12 * 1024 * 1024)
        offsets = {old: 0, active: 0}
        fdopen = os.fdopen
        actual_bytes = [0]

        class CountReads:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def read(self, *args):
                data = self.handle.read(*args)
                actual_bytes[0] += len(data)
                return data

            def readline(self, *args):
                data = self.handle.readline(*args)
                actual_bytes[0] += len(data)
                return data

        for cycle in range(3):
            os.utime(active, (2_000_000_000 + cycle, 2_000_000_000 + cycle))
            actual_bytes[0] = 0
            with patch.object(self.collector, 'scan_file', wraps=self.collector.scan_file) as scanned, patch('codex_mem.usage.os.fdopen', side_effect=lambda *args, **kwargs: CountReads(fdopen(*args, **kwargs))):
                result = self.collector.collect(config=self.config, max_files=2)
            self.assertEqual(1, result['recent_files'])
            self.assertEqual(1, result['historical_files'])
            self.assertEqual({old, active}, {call.args[0] for call in scanned.call_args_list})
            self.assertEqual(COLLECTION_BYTE_BUDGET, sum(call.kwargs['max_bytes'] + SCAN_FINGERPRINT_BYTE_BUDGET for call in scanned.call_args_list))
            self.assertGreater(actual_bytes[0], COLLECTION_BYTE_BUDGET - 4 * SCAN_FINGERPRINT_BYTE_BUDGET)
            self.assertLessEqual(actual_bytes[0], COLLECTION_BYTE_BUDGET)
            for path in (old, active):
                checkpoint = self.collector.store.get_checkpoint(path)
                self.assertGreater(checkpoint['offset'], offsets[path])
                self.assertLessEqual(checkpoint['offset'] - offsets[path], COLLECTION_BYTE_BUDGET // 2)
                offsets[path] = checkpoint['offset']
        self.assertEqual(2, len(self.rows()))

    def test_collection_file_and_byte_caps_apply_to_both_lanes_together(self):
        for n in range(COLLECTION_FILE_LIMIT + 20):
            self.populate(f'{n:03d}.jsonl', f'thread-{n}', 1_500_000_000 + n)
        with patch.object(self.collector, 'scan_file', wraps=self.collector.scan_file) as scanned:
            result = self.collector.collect(config=self.config, max_files=COLLECTION_FILE_LIMIT * 2)
        self.assertEqual(COLLECTION_FILE_LIMIT, scanned.call_count)
        self.assertEqual(COLLECTION_FILE_LIMIT, result['files'])
        self.assertEqual(COLLECTION_BYTE_BUDGET, sum(call.kwargs['max_bytes'] + SCAN_FINGERPRINT_BYTE_BUDGET for call in scanned.call_args_list))
        self.assertTrue(all(call.kwargs['max_bytes'] > 0 for call in scanned.call_args_list))

    def test_historical_scan_still_checks_replacement_when_metadata_is_unchanged(self):
        self.populate('active.jsonl', 'thread-a', 2_000_000_000)
        self.collector.collect(config=self.config, max_files=1)
        before = self.path.stat()
        self.write(self.meta(), self.turn(), self.native('response-b'), mode='w')
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(before.st_size, self.path.stat().st_size)
        self.collector.collect(config=self.config, max_files=1)
        self.assertEqual({'response-a', 'response-b'}, {row['response_id'] for row in self.rows()})

    def test_native_current_day_discovery_bypasses_large_archive_walk(self):
        archive = self.home / 'archived_sessions'
        archive.mkdir()
        for n in range(DISCOVERY_ENTRY_BUDGET + 1):
            (archive / f'{n:04d}.jsonl').touch()
        day = datetime.now(timezone.utc).strftime('%Y/%m/%d')
        self.populate(f'{day}/current.jsonl', 'active', 2_000_000_000)
        self.collector.collect(config=self.config, max_files=2)
        self.assertIn('active', {row['thread_id'] for row in self.rows()})
        self.assertIsNotNone(self.collector._discovery)

    def test_excluded_recent_source_is_reconsidered_when_scope_changes(self):
        for n in range(5):
            self.populate(f'{n:03d}-old.jsonl', f'old-{n}', 1_500_000_000 + n)
        self.populate('yyy-active.jsonl', 'active', 1_900_000_000)
        excluded = self.populate('zzz-excluded.jsonl', 'excluded', 2_000_000_000, project='/excluded')
        self.config['excluded_projects'] = ['/excluded']
        self.collector.collect(config=self.config, max_files=2)
        self.assertNotIn(excluded, self.collector._recent)
        self.assertNotIn('excluded', {row['thread_id'] for row in self.rows()})
        self.collector.collect(config=self.config, max_files=2)
        self.assertIn('active', {row['thread_id'] for row in self.rows()})
        self.config['excluded_projects'] = []
        self.collector.collect(config=self.config, max_files=2)
        self.assertIn('excluded', {row['thread_id'] for row in self.rows()})

    def test_historical_scan_rechecks_excluded_source_replacement(self):
        self.populate('active.jsonl', 'thread-a', 2_000_000_000, project='/excluded')
        self.config['excluded_projects'] = ['/excluded']
        self.collector.collect(config=self.config, max_files=1)
        self.assertEqual([], self.rows())
        before = self.path.stat()
        self.write(self.meta(cwd='/allowedx'), self.turn(), self.native(), mode='w')
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(before.st_size, self.path.stat().st_size)
        self.collector.collect(config=self.config, max_files=1)
        self.assertEqual(1, len(self.rows()))

    def test_period_refresh_finds_old_active_session_and_honors_scope(self):
        stamp = datetime(2026, 9, 12, tzinfo=timezone.utc).timestamp()
        self.populate('2025/01/01/old-active.jsonl', 'old-active', stamp, project='/selected')
        self.populate('2026/09/12/other.jsonl', 'other', stamp, project='/other')
        result = self.collector.refresh_period('2026-09-11T21:00:00Z', '2026-09-12T21:00:00Z',
                                               self.config, project='/selected', max_files=4)
        self.assertEqual({'old-active'}, {r['thread_id'] for r in self.rows()})
        self.assertEqual('global', result['coverage']['scope'])
        self.assertLessEqual(result['files'], 4)

    def test_period_refresh_budget_and_partial_tail_coverage(self):
        self.write(self.meta(), self.turn(), self.native())
        with self.path.open('ab') as out:
            out.write(b'{"incomplete"')
        start, end = '2026-01-01T00:00:00Z', '2100-01-01T00:00:00Z'
        result = self.collector.refresh_period(start, end, self.config, max_bytes=128)
        self.assertEqual(0, result['files'])
        self.assertEqual([], self.rows())
        result = self.collector.refresh_period(start, end, self.config, max_bytes=65536)
        self.assertEqual(1, len(self.rows()))
        self.assertEqual(1, result['coverage']['unread_files'])
        self.assertEqual(len(b'{"incomplete"'), result['coverage']['unread_bytes'])
        self.assertEqual('partial', result['coverage']['freshness'])

    def test_cached_coverage_is_read_only_and_marks_undiscovered_sources_unknown(self):
        self.write(self.meta(), self.turn(), self.native())
        self.scan()
        conn = self.collector.store.store._connection
        before = conn.total_changes
        coverage = coverage_period(self.collector.data_dir, '2026-01-01T00:00:00Z', '2100-01-01T00:00:00Z', codex_home=self.home)
        self.assertEqual('unknown', coverage['freshness'])
        self.assertFalse(coverage['discovery_complete'])
        self.assertEqual(0, coverage['unread_bytes'])
        self.assertEqual(before, conn.total_changes)
        missing = Path(self.tmp.name) / 'does-not-exist'
        self.assertEqual('missing_database', coverage_period(missing, '2026-01-01T00:00:00Z', '2100-01-01T00:00:00Z')['status'])
        self.assertFalse(missing.exists())

    def test_coverage_marks_old_parser_as_pending_repair_and_tracks_malformed_records(self):
        self.write(self.meta(), self.turn(), self.native())
        with self.path.open('ab') as out:
            out.write(b'bad json\n')
        self.scan()
        coverage = coverage_period(self.collector.data_dir, '2026-01-01T00:00:00Z', '2100-01-01T00:00:00Z', codex_home=self.home)
        self.assertEqual(1, coverage['malformed_records'])
        checkpoint = self.collector.store.get_checkpoint(self.path)
        checkpoint['parser_state'].pop('parser_version')
        self.collector.store.store._connection.execute('UPDATE usage_scan_files SET parser_state=?', (json.dumps(checkpoint['parser_state']),))
        coverage = coverage_period(self.collector.data_dir, '2026-01-01T00:00:00Z', '2100-01-01T00:00:00Z', codex_home=self.home)
        self.assertEqual(1, coverage['repair_files'])
        self.assertEqual(self.path.stat().st_size, coverage['repair_bytes'])

    def test_public_refresh_restarts_progress_beyond_discovery_entry_budget(self):
        from codex_mem.usage_api import usage_refresh
        directory = self.home / 'archived_sessions'
        directory.mkdir()
        total = DISCOVERY_ENTRY_BUDGET + 52
        for n in range(total):
            self.path = directory / f'{n:05d}.jsonl'
            self.write(self.meta(id=f'archive-{n}'), self.turn(), self.native(thread=f'archive-{n}'))
        data_dir = Path(self.collector.data_dir)
        (data_dir / 'config.json').write_text(json.dumps(self.config))
        for _ in range(24):
            usage_refresh(data_dir, from_date='2026-09-11', to_date='2026-09-13', codex_home=self.home, max_files=128)
            if len(self.rows()) == total:
                break
        self.assertEqual(total, len(self.rows()))
        self.assertEqual(total * 120, sum(row['total_tokens'] for row in self.rows()))

    def test_snapshot_overflow_is_reported_as_incomplete(self):
        for n in range(5):
            self.populate(f'{n}.jsonl', f'thread-{n}', 2_000_000_000)
        with patch('codex_mem.usage.REFRESH_SNAPSHOT_ENTRY_LIMIT', 2):
            result = self.collector.refresh_period('2026-09-11T00:00:00Z', '2026-09-13T00:00:00Z', self.config)
        self.assertGreater(result['coverage']['discovery_overflow_directories'], 0)
        self.assertFalse(result['coverage']['discovery_complete'])
        self.assertEqual('partial', result['coverage']['freshness'])

    def test_public_refresh_removes_vanished_or_disallowed_queue_entries(self):
        from codex_mem.usage_api import usage_refresh
        for change in ('deleted', 'symlink', 'outside'):
            with self.subTest(change=change), tempfile.TemporaryDirectory(dir=self.tmp.name) as temporary:
                base = Path(temporary)
                home, data = base / 'codex', base / 'memory'
                archive = home / 'archived_sessions'
                archive.mkdir(parents=True)
                with closing(UsageCollector(data, home)) as collector:
                    (data / 'config.json').write_text(json.dumps(self.config))

                    def write_file(path, thread):
                        records = [self.meta(id=thread), self.turn(), self.native(thread=thread)]
                        path.write_text(''.join(json.dumps({'type': kind, 'payload': payload}) + '\n' for kind, payload in records))

                    def refresh():
                        return usage_refresh(data, from_date='2026-09-11', to_date='2026-09-13', codex_home=home, max_files=1)

                    for n in range(2):
                        write_file(archive / f'{n}.jsonl', f'archive-{n}')
                    refresh()
                    conn = collector.store.store._connection
                    queued = Path(conn.execute("SELECT path FROM usage_discovery_queue WHERE kind='file'").fetchone()[0])
                    queued.unlink()
                    outside = base / 'outside.jsonl'
                    write_file(outside, 'must-not-be-read')
                    if change == 'symlink':
                        queued.symlink_to(outside)
                    elif change == 'outside':
                        conn.execute("UPDATE usage_discovery_queue SET path=? WHERE path=?", (str(outside), str(queued)))
                    write_file(archive / 'new.jsonl', 'new-thread')
                    for _ in range(3):
                        refresh()
                    threads = {row['thread_id'] for row in collector.store.list_events()}
                    self.assertIn('new-thread', threads)
                    self.assertNotIn('must-not-be-read', threads)
                    self.assertEqual(2, len(threads))

    def test_public_refresh_retries_snapshot_overflow_in_later_call(self):
        from codex_mem.usage_api import usage_refresh
        archive = self.home / 'archived_sessions'
        archive.mkdir()
        for n in range(2):
            self.path = archive / f'{n}.jsonl'
            self.write(self.meta(id=f'archive-{n}'), self.turn(), self.native(thread=f'archive-{n}'))
        data = Path(self.collector.data_dir)
        (data / 'config.json').write_text(json.dumps(self.config))
        arguments = dict(from_date='2026-09-11', to_date='2026-09-13', codex_home=self.home, max_files=128)
        with patch('codex_mem.usage.REFRESH_SNAPSHOT_ENTRY_LIMIT', 1):
            initial = usage_refresh(data, **arguments)
        self.assertGreater(initial['coverage']['discovery_overflow_directories'], 0)
        for _ in range(3):
            result = usage_refresh(data, **arguments)
        self.assertEqual(2, len(self.rows()))
        self.assertEqual({}, result['coverage']['pending_discovery'])
        self.assertTrue(result['coverage']['discovery_complete'])

    def test_public_refresh_completed_inventory_reports_current_after_scan(self):
        from codex_mem.usage_api import usage_refresh
        self.path = self.home / 'archived_sessions' / 'single.jsonl'
        self.path.parent.mkdir()
        self.write(self.meta(), self.turn(), self.native())
        data = Path(self.collector.data_dir)
        (data / 'config.json').write_text(json.dumps(self.config))
        for _ in range(3):
            result = usage_refresh(data, from_date='2026-09-11', to_date='2026-09-13', codex_home=self.home, max_files=128)
            self.assertTrue(result['coverage']['discovery_complete'])
            self.assertEqual({}, result['coverage']['pending_discovery'])
            self.assertEqual('known_sources_current', result['coverage']['freshness'])


if __name__ == '__main__':
    unittest.main()
