"""Usage accounting invariants and cursor transaction behavior."""
import tempfile
import unittest

from codex_mem.store import SCHEMA_VERSION, StoreError
from codex_mem.usage_store import UsageStore


class UsageStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = UsageStore(self.tmp.name)
        self.session = {"thread_id": "root", "session_id": "root", "project": self.tmp.name}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def event(self, **changes):
        event = dict(event_key="root:r1", thread_id="root", session_id="root", response_id="r1", source_kind="response", model="model-a", input_tokens=100, cached_input_tokens=40, cache_write_input_tokens=0, output_tokens=20, reasoning_output_tokens=5, total_tokens=120)
        event.update(changes)
        return event

    def commit(self, events, expected=None, offset=100, path="rollout", session=None):
        return self.store.commit_scan(path, expected, {"offset": offset, "file_identity": "1:2", "parser_state": {}}, session or self.session, events)

    def test_replay_and_response_identity_do_not_double_count(self):
        self.assertTrue(self.commit([self.event()]))
        self.assertTrue(self.commit([self.event(event_key="different-key")], 100, 50))
        totals = self.store.usage_totals()
        self.assertEqual(1, totals[0]["event_count"])
        self.assertEqual(120, totals[0]["total_tokens"])
        self.assertEqual(50, self.store.get_checkpoint("rollout")["offset"])
        self.assertEqual(SCHEMA_VERSION, 4)

    def test_replay_enriches_unknown_model_without_rewriting_counters(self):
        self.commit([self.event(model=None, model_source="unknown")])
        self.commit([self.event(model="known", model_source="turn_context")], 100, 200)
        row = self.store.usage_totals()[0]
        self.assertEqual("known", row["model"])
        source = self.store.store._connection.execute("SELECT model_source FROM usage_events").fetchone()[0]
        self.assertEqual("turn_context", source)
        self.assertEqual(1, row["event_count"])
        self.assertEqual(120, row["total_tokens"])

    def test_delayed_root_repairs_old_events_and_stale_files_cannot_revert(self):
        child = dict(self.session, thread_id="child", session_id="child")
        self.commit([self.event(thread_id="child", session_id="child")], session=child)
        identified = dict(child, session_id="root", parent_thread_id="root")
        self.commit([self.event(thread_id="child", event_key="r2", response_id="r2")], expected=100, offset=200, session=identified)
        self.assertEqual(240, self.store.usage_totals(session_id="root")[0]["total_tokens"])
        self.assertEqual([], self.store.usage_totals(session_id="child"))
        self.commit([self.event(thread_id="child", session_id="child", event_key="r3", response_id="r3")], path="stale-file", session=child)
        self.assertEqual(360, self.store.usage_totals(session_id="root")[0]["total_tokens"])
        self.assertEqual([], self.store.usage_totals(session_id="child"))
        saved = self.store.store._connection.execute("SELECT session_id FROM usage_sessions WHERE thread_id='child'").fetchone()[0]
        self.assertEqual("root", saved)

    def test_cas_rejects_stale_writer_without_writing_events(self):
        self.commit([self.event()])
        self.assertFalse(self.commit([self.event(event_key="r2", response_id="r2")], 0, 200))
        self.assertEqual(100, self.store.get_checkpoint("rollout")["offset"])
        self.assertEqual(1, self.store.usage_totals()[0]["event_count"])

    def test_sql_failure_rolls_back_session_events_and_cursor(self):
        with self.store.store._lock:
            self.store.store._connection.execute("CREATE TRIGGER fail_cursor BEFORE INSERT ON usage_scan_files BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(StoreError):
            self.commit([self.event()])
        self.assertEqual([], self.store.usage_totals())
        self.assertIsNone(self.store.get_checkpoint("rollout"))
        self.assertEqual(0, self.store.store._connection.execute("SELECT count(*) FROM usage_sessions").fetchone()[0])

    def test_aggregation_retains_root_agent_model_and_unknown(self):
        self.commit([self.event(), self.event(event_key="r2", response_id="r2", model=None)])
        child = dict(self.session, thread_id="child", parent_thread_id="root", agent_role="worker")
        self.commit([self.event(thread_id="child", event_key="c1", response_id="c1")], path="child-file", session=child)
        rows = self.store.usage_totals(project=self.tmp.name, session_id="root")
        self.assertEqual(3, len(rows))
        self.assertEqual(360, sum(row["total_tokens"] for row in rows))
        self.assertEqual(1, sum(row["model"] is None for row in rows))
        self.assertEqual({"root", "child"}, {row["thread_id"] for row in rows})
        self.assertEqual([], self.store.usage_totals(session_id="unrelated"))

    def test_native_replaces_legacy_and_legacy_replay_is_ignored(self):
        legacy = self.event(source_kind="legacy", response_id=None, event_key="legacy")
        self.commit([legacy])
        self.commit([self.event()], 100, 200)
        self.commit([legacy], 200, 300)
        rows = self.store.usage_totals()
        self.assertEqual(1, len(rows))
        self.assertEqual("response", rows[0]["source_kind"])
        self.assertEqual(120, rows[0]["total_tokens"])

    def test_rejects_invalid_numbers_and_subsets_without_checkpoint(self):
        for change in ({"input_tokens": True}, {"output_tokens": -1}, {"total_tokens": 120.0}, {"input_tokens": "100"}, {"cached_input_tokens": 101}, {"reasoning_output_tokens": 21}, {"total_tokens": 121}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.commit([self.event(**change)])
        self.assertIsNone(self.store.get_checkpoint("rollout"))

    def test_rejects_narrative_state_and_foreign_owner(self):
        with self.assertRaises(ValueError):
            self.store.commit_scan("file", None, {"offset": 1, "parser_state": {"prompt": "secret"}}, self.session, [])
        with self.assertRaises(ValueError):
            self.commit([self.event(thread_id="foreign")])

    def test_checkpoint_roundtrip_and_multiple_writers(self):
        checkpoint = {"offset": 5, "fingerprint": "abc", "parser_state": {"turn_models": {"turn-1": {"model": "x"}}, "native_seen": True}}
        self.store.commit_scan("file", None, checkpoint, self.session, [])
        with UsageStore(self.tmp.name) as other:
            self.assertEqual(checkpoint["parser_state"], other.get_checkpoint("file")["parser_state"])
            self.assertFalse(other.commit_scan("file", None, checkpoint, self.session, []))
            self.assertEqual(1, len(other.list_checkpoints()))

    def test_time_window_normalizes_offsets_and_preserves_microsecond_boundaries(self):
        stamps = ('2026-09-11T23:59:59.999999+03:00', '2026-09-12T00:00:00+03:00',
                  '2026-09-12T20:59:59.999999Z', '2026-09-12T21:00:00Z')
        self.commit([self.event(event_key=f'r{i}', response_id=f'r{i}', recorded_at=stamp) for i, stamp in enumerate(stamps)])
        rows = self.store.list_events(start_at='2026-09-12T00:00:00+03:00', end_at='2026-09-13T00:00:00+03:00')
        self.assertEqual(['r1', 'r2'], [r['response_id'] for r in rows])
        totals = self.store.usage_totals(start_at='2026-09-12T00:00:00+03:00', end_at='2026-09-13T00:00:00+03:00')
        self.assertEqual(240, totals[0]['total_tokens'])
        plan = self.store.store._connection.execute('EXPLAIN QUERY PLAN SELECT * FROM usage_events WHERE recorded_at>=? AND recorded_at<?', ('2026-09-11', '2026-09-13')).fetchall()
        self.assertIn('usage_recorded_at', str([tuple(row) for row in plan]))
        with self.assertRaises(ValueError):
            self.store.list_events(start_at='2026-09-13T00:00:00Z', end_at='2026-09-12T00:00:00Z')
        with self.assertRaises(ValueError):
            self.store.list_events(start_at='2026-09-12')

    def test_ledger_coverage_separates_requested_unknown_legacy_and_undated(self):
        self.commit([self.event(recorded_at='2026-09-12T00:00:00Z', requested_service_tier='priority'),
                     self.event(event_key='r2', response_id='r2', model=None, recorded_at='2026-09-12T01:00:00Z'),
                     self.event(event_key='r3', response_id='r3')])
        coverage = self.store.ledger_coverage(start_at='2026-09-12T00:00:00Z', end_at='2026-09-13T00:00:00Z')
        self.assertEqual(2, coverage['event_count'])
        self.assertEqual(1, coverage['unknown_model_events'])
        self.assertEqual(1, coverage['unknown_tier_events'])
        self.assertEqual(1, coverage['requested_tier_events'])
        self.assertEqual(1, coverage['undated_events_outside_period'])

    def test_unproven_tier_from_old_writer_keeps_requested_meaning(self):
        self.commit([self.event(service_tier='priority')])
        row = self.store.list_events()[0]
        self.assertIsNone(row['service_tier'])
        self.assertEqual('priority', row['requested_service_tier'])
        self.assertEqual('thread_settings_legacy', row['requested_service_tier_source'])
        self.commit([self.event(service_tier='default', service_tier_source='token_usage_record')], 100, 200)
        row = self.store.list_events()[0]
        self.assertEqual('priority', row['requested_service_tier'])
        self.assertEqual('default', row['service_tier'])
        self.assertEqual(120, row['total_tokens'])

    def test_old_usage_schema_migration_preserves_tiers_and_counters(self):
        conn = self.store.store._connection
        self.commit([self.event(recorded_at='2026-09-12T03:00:00+03:00')])
        for operation in ('insert', 'update'):
            conn.execute(f'DROP TRIGGER usage_tier_provenance_{operation}')
        for name in ('requested_service_tier', 'requested_service_tier_source', 'service_tier_source'):
            conn.execute(f'ALTER TABLE usage_events DROP COLUMN {name}')
        conn.execute("UPDATE usage_events SET service_tier='priority',recorded_at='2026-09-12T03:00:00+03:00'")
        with UsageStore(self.tmp.name) as migrated:
            row = migrated.list_events()[0]
            self.assertEqual('priority', row['requested_service_tier'])
            self.assertIsNone(row['service_tier'])
            self.assertEqual('2026-09-12T00:00:00.000000Z', row['recorded_at'])
            self.assertEqual(120, row['total_tokens'])

    def test_old_writer_timestamp_width_and_offset_use_exact_indexed_ranges(self):
        self.commit([self.event(recorded_at='2026-09-12T00:00:00.123000Z'),
                     self.event(event_key='r2', response_id='r2', recorded_at='2026-09-12T00:00:00.123456Z')])
        conn = self.store.store._connection
        conn.execute("UPDATE usage_events SET recorded_at='2026-09-12T03:00:00.123+03:00' WHERE response_id='r1'")
        args = {'start_at': '2026-09-12T00:00:00.123000Z', 'end_at': '2026-09-12T00:00:00.123456Z'}
        self.assertEqual(['r1'], [row['response_id'] for row in self.store.list_events(**args)])
        self.assertEqual(120, self.store.usage_totals(**args)[0]['total_tokens'])
        self.assertEqual(1, self.store.ledger_coverage(**args)['event_count'])
        where, bound = self.store._filters(**args)
        plan = conn.execute('EXPLAIN QUERY PLAN SELECT e.event_key FROM usage_events e JOIN usage_sessions s ON s.thread_id=e.thread_id' + where, bound).fetchall()
        self.assertIn('usage_recorded_time', str([tuple(row) for row in plan]))


if __name__ == "__main__":
    unittest.main()
