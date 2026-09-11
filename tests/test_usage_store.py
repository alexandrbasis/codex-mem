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


if __name__ == "__main__":
    unittest.main()
