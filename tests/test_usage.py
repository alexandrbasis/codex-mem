"""Accounting regression tests use synthetic rollouts, never the user's DB."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_mem.usage import COLLECTION_BYTE_BUDGET, COLLECTION_FILE_LIMIT, DISCOVERY_ENTRY_BUDGET, SCAN_FINGERPRINT_BYTE_BUDGET, UsageCollector


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


if __name__ == '__main__':
    unittest.main()
