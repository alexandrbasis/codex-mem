"""Freshness behavior on isolated stores; never touches installed memory."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_mem.config import configure
from codex_mem.freshness import freshness_snapshot
from codex_mem.store import Store, StoreError


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = (self.base / 'project').resolve()
        self.store = Store(self.base / 'memory')
        self.addCleanup(self.store.close)
        configure(self.store.data_dir, capture_scope='all', semantic_enabled=False)

    def snapshot(self):
        return freshness_snapshot(self.store, self.project)

    def test_empty_is_known_empty_without_fabricated_timestamps(self):
        result = self.snapshot()
        self.assertEqual(result['status'], 'empty')
        self.assertFalse(result['knowledge_incomplete'])
        self.assertEqual(result['pending_capture_count'], 0)
        self.assertIsNone(result['last_successful_processing_at'])
        self.assertIsNone(result['latest_capture_at'])

    def test_idle_old_project_is_current_and_other_project_backlog_excluded(self):
        entry = self.store.remember(self.project, 'note', 'ready')
        self.store._connection.execute("UPDATE entries SET created_at='2020-01-01T00:00:00Z', updated_at='2020-01-01T00:00:00Z' WHERE id=?", (entry['id'],))
        self.store.remember(self.base / 'other', 'capture', 'new', source='hook:Stop')
        result = self.snapshot()
        self.assertEqual(result['status'], 'current')
        self.assertEqual(result['last_ready_at'], '2020-01-01T00:00:00Z')
        self.assertEqual(result['pending_capture_count'], 0)

    def test_full_index_does_not_hide_849_pending_captures(self):
        for number in range(849):
            self.store.remember(self.project, str(number), 'raw', source='hook:Stop')
        with patch.object(self.store, 'embedding_status', return_value={'indexed': 399, 'pending': 0, 'stale': 0}):
            result = self.snapshot()
        self.assertEqual(result['status'], 'lagging')
        self.assertTrue(result['knowledge_incomplete'])
        self.assertEqual(result['pending_capture_count'], 849)
        self.assertEqual(result['index']['indexed'], 399)
        self.assertIn('pending captures=849', result['summary'])

    def test_blocked_state_is_scoped_and_codes_are_safe(self):
        from codex_mem.service import _new_state, _new_record
        state = _new_state()
        record = _new_record(1.0)
        record.update(blocked=True, last_code='runner_failure')
        state['projects'][str(self.project)] = record
        with patch('codex_mem.service._load_state_readonly', return_value=state):
            result = self.snapshot()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['blocked_reason'], 'runner_failure')
        record['last_code'] = 'secret body'
        with patch('codex_mem.service._load_state_readonly', return_value=state):
            self.assertNotIn('secret body', json.dumps(self.snapshot()))

    def test_invalid_config_and_missing_index_are_unknown(self):
        (self.store.data_dir / 'config.json').write_text('{')
        with patch.object(self.store, 'embedding_status', side_effect=StoreError('unavailable')):
            result = self.snapshot()
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['capture_enabled'])
        self.assertIsNone(result['index']['indexed'])
        self.assertIsNone(result['knowledge_incomplete'])

    def test_disabled_is_not_healthy(self):
        configure(self.store.data_dir, capture_enabled=False)
        result = self.snapshot()
        self.assertEqual(result['status'], 'disabled')
        self.assertIsNone(result['knowledge_incomplete'])

    def test_reads_do_not_mutate_sqlite_or_files(self):
        before = {p.name: p.read_bytes() for p in self.store.data_dir.iterdir() if p.is_file() and not p.name.endswith('-shm')}
        changes = self.store._connection.total_changes
        self.snapshot()
        after = {p.name: p.read_bytes() for p in self.store.data_dir.iterdir() if p.is_file() and not p.name.endswith('-shm')}
        self.assertEqual(before, after)
        self.assertEqual(changes, self.store._connection.total_changes)

    def test_failed_receipt_blocks_until_successful_retry(self):
        self.store.remember(self.project, 'capture', 'input', source='hook:Stop')
        job = self.store.claim_observation_batch(self.project, 'fixture', 'gpt-5.6-luna', 'medium')
        self.store.fail_observation_batch(self.project, job['job_id'], job['lease_token'], 'runner_failure')
        result = self.snapshot()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['pending_capture_count'], 1)
        retry = self.store.claim_observation_batch(self.project, 'fixture', 'gpt-5.6-luna', 'medium', retry_failed=True)
        self.store.finish_observation_batch(self.project, retry['job_id'], retry['lease_token'], disposition='skipped')
        result = self.snapshot()
        self.assertEqual(result['status'], 'current')
        self.assertEqual(result['pending_capture_count'], 0)
        self.assertIsNotNone(result['last_successful_processing_at'])
        self.assertIsNone(result['last_ready_at'])
        self.assertIsNone(result['blocked_reason'])

    def test_index_backlog_is_distinct_from_capture_backlog(self):
        configure(self.store.data_dir, semantic_enabled=True)
        self.store.remember(self.project, 'ready', 'a note')
        result = self.snapshot()
        self.assertEqual(result['status'], 'lagging')
        self.assertEqual(result['pending_capture_count'], 0)
        self.assertEqual(result['index']['pending'], 1)
        self.assertIsNotNone(result['last_ready_at'])

    def test_corrupt_service_state_preserves_unknown(self):
        (self.store.data_dir / 'service-state.json').write_text('{')
        result = self.snapshot()
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['knowledge_incomplete'])
        self.assertIn('blocker=unknown', result['summary'])

    def test_unavailable_store_does_not_break_retrieval_telemetry(self):
        result = freshness_snapshot(object(), self.project)
        self.assertEqual(result['status'], 'unknown')
        self.assertIsNone(result['pending_capture_count'])
        self.assertIsNone(result['ready_count'])
        self.assertLess(len(result['summary']), 850)

    def test_disabled_semantic_index_does_not_mark_ready_memory_lagging(self):
        self.store.remember(self.project, 'ready', 'note')
        result = self.snapshot()
        self.assertEqual(result['status'], 'current')
        self.assertFalse(result['semantic_enabled'])
        self.assertEqual(result['index']['pending'], 1)
        self.assertIn('capture/processing/semantic enabled=True/True/False', result['summary'])

    def test_disabled_processor_with_backlog_exposes_both_facts(self):
        configure(self.store.data_dir, processor_enabled=False)
        self.store.remember(self.project, 'capture', 'raw', source='hook:Stop')
        result = self.snapshot()
        self.assertEqual(result['status'], 'lagging')
        self.assertTrue(result['knowledge_incomplete'])
        self.assertFalse(result['processing_enabled'])
        self.assertIn('capture/processing/semantic enabled=True/False/False', result['summary'])
