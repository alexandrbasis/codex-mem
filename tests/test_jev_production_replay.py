"""Private production replay never mutates the source store or reports source text."""
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from codex_mem import jev_cache, jev_filter
from codex_mem.store import Store, project_key
from tests.test_jev_short_circuit import answer

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/jev_production_replay.py"
SPEC = importlib.util.spec_from_file_location("jev_production_replay", SCRIPT)
replay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(replay)


class JevProductionReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "memory"
        self.project = project_key(self.root / "project")
        with Store(self.home) as store:
            source = store.remember(self.project, "PRIVATE_SOURCE_TITLE",
                                    "PRIVATE_SOURCE_BODY " + "x" * 70_000, source="hook:PostToolUse")
        self.database = self.home / "memory.sqlite3"
        self.manifest = {"scope": self.project, "sources": [{"case": "p01", "source_id": source["id"]}]}
        self.labels = {"p01": "useful"}

    def prime_cache(self, required=False):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            sources, _ = replay.read_sample(connection, self.project, self.manifest)
        with Store(self.home) as store:
            for payload in jev_filter._payloads(sources[0], "sources", required):
                jev_cache.cache_put(store, self.project, payload, answer(True))

    def test_missing_responses_never_trigger_network_and_store_remains_identical(self):
        before = self.database.read_bytes()
        with mock.patch.object(jev_filter, "_post", side_effect=AssertionError("network not allowed")) as post:
            result = replay.run(self.database, self.project, self.manifest, self.labels)
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["missing_source_cases"], ["p01"])
            post.assert_not_called()
        self.assertEqual(before, self.database.read_bytes())

    def test_isolated_exact_replay_preserves_full_source_and_labels(self):
        self.prime_cache()
        before = self.database.read_bytes()
        result = replay.run(self.database, self.project, self.manifest, self.labels)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["live_requests"], 0)
        self.assertGreater(result["baseline_fragment_requests_without_cache"], 1)
        self.assertEqual(result["optimized_fragment_requests_without_cache"], 1)
        self.assertEqual(result["quality_counts"], {"useful_retain": 1})
        self.assertTrue(result["routes_unchanged"])
        self.assertTrue(result["retained_sources_unchanged"])
        self.assertEqual(result["warm_requests"], 0)
        self.assertTrue(result["warm_zero_tokens"])
        self.assertFalse(result["generator_executed"])
        self.assertIsNone(result["generator_calls_saved"])
        self.assertNotIn("PRIVATE_SOURCE", json.dumps(result))
        self.assertNotIn(self.manifest["sources"][0]["source_id"], json.dumps(result))
        self.assertEqual(before, self.database.read_bytes())

    def test_exact_cached_responses_need_no_network(self):
        self.prime_cache()
        with mock.patch.object(jev_filter, "_post", side_effect=AssertionError("network not needed")) as post:
            result = replay.run(self.database, self.project, self.manifest, self.labels)
            post.assert_not_called()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["live_requests"], 0)
        self.assertGreater(result["exact_cached_fragments"], 1)

    def test_historical_restore_requires_exact_complete_cache_match(self):
        self.prime_cache(required=True)
        with Store(self.home) as store:
            store.remember(self.project, "Derived", "Durable result",
                           source_ids=[self.manifest["sources"][0]["source_id"]])
        result = replay.run(self.database, self.project, self.manifest, self.labels)
        self.assertEqual(result["status"], "incomplete")
        result = replay.run(self.database, self.project, self.manifest, self.labels, historical_exact=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["source_views"], {"before_supersession": 1})
        with Store(self.home) as store:
            store._write(lambda: store._connection.execute("UPDATE entries SET body='Changed evidence' WHERE id=?",
                         (self.manifest["sources"][0]["source_id"],)))
        result = replay.run(self.database, self.project, self.manifest, self.labels, historical_exact=True)
        self.assertEqual(result["status"], "incomplete")

    def test_changed_or_cross_project_sources_do_not_inherit_labels(self):
        self.manifest["sources"][0]["hydrated_source_sha256"] = "old-snapshot"
        with self.assertRaisesRegex(ValueError, "changed_since_labeling"):
            replay.run(self.database, self.project, self.manifest, self.labels)
        with self.assertRaisesRegex(ValueError, "project_mismatch"):
            replay.run(self.database, "/tmp/another-project", self.manifest, self.labels)

    def test_historical_route_mismatch_fails_and_missing_receipt_is_incomplete(self):
        self.prime_cache()
        source_id = self.manifest['sources'][0]['source_id']
        self.manifest['sources'][0]['job_id'] = 'historical-job'
        audit = {'decisions': [{'source_id': source_id, 'location': 'sources', 'route': 'discard'}]}
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("INSERT INTO observation_jobs(id,project,processor_id,model,reasoning_effort,"
                               "input_fingerprint,input_limit,status,attempt_count,output_ids_json,created_at,updated_at) "
                               "VALUES('historical-job',?,'observer','test','low','digest',100,'skipped',1,'[]','now','now')",
                               (self.project,))
            connection.execute("CREATE TABLE jev_filter_attempts(job_id TEXT,attempt_count INTEGER,audit_json TEXT,updated_at TEXT)")
            connection.execute("INSERT INTO jev_filter_attempts VALUES('historical-job',1,?,'now')", (json.dumps(audit),))
            connection.commit()
        result = replay.run(self.database, self.project, self.manifest, self.labels)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['historical_routes_match_replay'])
        self.assertEqual(result['historical_route_mismatches'], ['p01'])
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM jev_filter_attempts")
            connection.commit()
        result = replay.run(self.database, self.project, self.manifest, self.labels)
        self.assertEqual(result['status'], 'incomplete')
        self.assertIsNone(result['historical_routes_match_replay'])
        self.assertEqual(result['historical_provenance_status'], 'incomplete')
        self.assertEqual(result['expected_historical_source_routes'], 1)

    def test_missing_database_is_not_created(self):
        absent = self.root / "absent.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            replay.run(absent, self.project, self.manifest, self.labels)
        self.assertFalse(absent.exists())


if __name__ == "__main__":
    unittest.main()
