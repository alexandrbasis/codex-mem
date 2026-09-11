"""Accounting regression tests use synthetic rollouts, never the user's DB."""
import json
from pathlib import Path
import tempfile
import unittest

from codex_mem.usage import UsageCollector


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


if __name__ == '__main__':
    unittest.main()
