"""Evaluation receipts distinguish quality failures, API failures and unknown cost."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_mem import jev_filter
from codex_mem.store import project_key

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/jev_quality_eval.py"
SPEC = importlib.util.spec_from_file_location("jev_quality_eval", SCRIPT)
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


def answer(retain, category="routine"):
    return {"model": jev_filter.MODEL, "answers": {
        "useful": {"type": "noul", "noul": 0.99 if retain else 0.01},
        "category": {"type": "choice", "choice": category, "confidence": 0.99,
                     "probabilities": {key: float(key == category) for key in jev_filter.CATEGORIES}},
    }, "usage": {"input_tokens": 100, "output_tokens": 10}}


class JevQualityEvaluationTests(unittest.TestCase):
    def test_labels_are_not_sent_and_real_cache_avoids_repeat_requests(self):
        fixture = evaluation.load_fixture(evaluation.DEFAULT_FIXTURE)
        labels = {case["id"]: case for case in fixture["cases"]}
        calls = []

        def evaluate(payload):
            source = json.loads(payload["state"]["source_fragment"])
            self.assertNotIn("expected", source)
            self.assertNotIn("reason", source)
            self.assertNotIn("category", source)
            calls.append(source["id"])
            case = labels[source["id"]]
            return answer(case["expected"] == "retain", case["category"])

        report = evaluation.evaluate_fixture(fixture, evaluator=evaluate)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["evidence_mode"], "test_double")
        self.assertEqual(report["confusion"]["useful_discarded"], 0)
        self.assertEqual(report["confusion"]["routine_retained"], 0)
        self.assertEqual(report["cache"]["warm_requests"], 0)
        self.assertEqual(report["cache"]["warm_hits"], len(fixture["cases"]))
        self.assertTrue(report["cache"]["warm_zero_tokens"])
        self.assertTrue(report["cache"]["routes_unchanged"])
        self.assertEqual(report["batch_gate"]["would_skip_generator"], 5)
        self.assertEqual(report["batch_gate"]["wrongful_skips"], 0)
        summary = next(row for row in report["batches"] if row["batch"] == "required_session_summary")
        self.assertTrue(summary["generator_required"])
        self.assertEqual(summary["audit"]["lifecycle_only_ids"], ["bare_done"])
        self.assertEqual(report["jev_usage"]["input_tokens"], len(calls) * 100)
        self.assertIsNone(report["generator"]["counterfactual_saved_tokens"])
        self.assertFalse(report["generator"]["executed"])

    def test_dropped_useful_event_is_reported_even_if_execution_completes(self):
        fixture = evaluation.load_fixture(evaluation.DEFAULT_FIXTURE)
        fixture["batches"] = [batch for batch in fixture["batches"] if not batch["summary_required"]]
        report = evaluation.evaluate_fixture(fixture, evaluator=lambda payload: answer(False))
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["confusion"]["useful_discarded"], 18)
        self.assertEqual(report["confusion"]["useful_recall"], 0)
        self.assertGreater(report["batch_gate"]["wrongful_skips"], 0)

    def test_api_failure_is_not_a_passing_mock_or_zero_cost(self):
        fixture = evaluation.load_fixture(evaluation.DEFAULT_FIXTURE)

        def fail(payload):
            raise jev_filter.JevFilterError("jev_filter_transport")

        report = evaluation.evaluate_fixture(fixture, evaluator=fail)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["code"], "jev_filter_transport")
        self.assertEqual(report["usage_completeness"], "partial_or_unavailable")
        self.assertNotIn("confusion", report)
        self.assertEqual(report["batches"], [])
        self.assertIsNone(report["cost_usd"])

    def test_partial_paid_usage_survives_invalid_response(self):
        fixture = evaluation.load_fixture(evaluation.DEFAULT_FIXTURE)
        report = evaluation.evaluate_fixture(fixture, evaluator=lambda payload: {
            "model": "unexpected", "usage": {"input_tokens": 321, "output_tokens": 4}})
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["jev_usage"], {"input_tokens": 321, "output_tokens": 4})
        self.assertEqual(report["code"], "jev_filter_invalid_response")

    def test_confusion_keeps_missing_decisions_distinct_from_discard(self):
        result = evaluation.confusion([
            {"expected": "retain", "actual": "unresolved"},
            {"expected": "discard", "actual": "retain"},
        ])
        self.assertEqual(result["unresolved"], 1)
        self.assertEqual(result["useful_discarded"], 0)
        self.assertIsNone(result["useful_recall"])

    def test_optional_gate_latency_distinguishes_old_receipts_from_zero(self):
        report = evaluation.duration_metrics([{"duration_ms": 20}, {}, {"duration_ms": 0}])
        self.assertEqual(report["measured_duration_ms"], 20)
        self.assertEqual(report["duration_missing_attempts"], 1)
        self.assertIsNone(report["complete_duration_ms"])
        self.assertEqual(evaluation.duration_metrics([{"duration_ms": 20}])["complete_duration_ms"], 20)

    def test_jev_price_is_model_specific_and_output_tokens_are_not_charged(self):
        self.assertEqual(evaluation.jev_cost(1_000_000, "jev-1.13.0")["observed_input_cost_usd"], "0.042")
        self.assertIsNone(evaluation.jev_cost(1_000_000, "new-unknown-model")["observed_input_cost_usd"])

    def test_production_reads_only_metadata_and_preserves_unknowns(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "prod.sqlite3"
            project = Path(directory) / "project"
            with sqlite3.connect(database) as connection:
                connection.executescript("""
                    CREATE TABLE observation_jobs(id TEXT,project TEXT,status TEXT,
                        disposition TEXT,attempt_count INTEGER,model TEXT,reasoning_effort TEXT);
                    CREATE TABLE jev_filter_attempts(job_id TEXT,attempt_count INTEGER,
                        audit_json TEXT,updated_at TEXT);
                    CREATE TABLE observer_usage_attempts(job_id TEXT,attempt_count INTEGER,
                        outcome TEXT,usage_status TEXT,duration_ms INTEGER,started_at TEXT,
                        input_tokens INTEGER,cached_input_tokens INTEGER,cache_write_input_tokens INTEGER,
                        output_tokens INTEGER,reasoning_output_tokens INTEGER,total_tokens INTEGER);
                    CREATE TABLE private_records(body TEXT);
                    INSERT INTO private_records VALUES('PRIVATE BODY MUST NOT ENTER REPORT');
                """)
                for job_id, status in (("j1", "skipped"), ("j2", "processed"), ("j3", "failed")):
                    connection.execute("INSERT INTO observation_jobs VALUES(?,?,?,?,?,?,?)",
                                       (job_id, project_key(project), status, status, 1, "gpt-5.6-luna", "medium"))
                    audit = {"status": "success", "generator_started": job_id != "j1",
                             "usage": {"input_tokens": 100, "output_tokens": 10},
                             "counts": {"evaluated": 2, "discarded": 1, "retained": 1}}
                    connection.execute("INSERT INTO jev_filter_attempts VALUES(?,?,?,?)",
                                       (job_id, 1, json.dumps(audit), "2026-09-20T12:00:00Z"))
                connection.execute("INSERT INTO observer_usage_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("j2", 1, "processed", "reported", 1500, "2026-09-20T12:00:01Z",
                                    1000, 300, 0, 100, 20, 1100))
                connection.execute("INSERT INTO observer_usage_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                   ("j3", 1, "failed", "unavailable", None, "2026-09-20T12:00:01Z",
                                    None, None, None, None, None, None))
            before = database.read_bytes()
            report = evaluation.read_production_telemetry(database, project, "2026-09-19T00:00:00Z")
            self.assertEqual(before, database.read_bytes())
            self.assertEqual(report["jev"]["confirmed_skipped_batches"], 1)
            self.assertEqual(report["generator"]["tokens_reported"]["total_tokens"], 1100)
            self.assertEqual(report["generator"]["duration_missing_attempts"], 1)
            prices = report["generator"]["api_equivalent_usd"]
            self.assertEqual(prices["standard_scenario_subtotal"], "0.000266")
            self.assertIsNone(prices["standard_scenario_total"])
            self.assertIsNone(prices["selected_total"])
            self.assertEqual(prices["unpriced_attempts"], 1)
            self.assertEqual(report["jev"]["duration_missing_attempts"], 3)
            self.assertIsNone(report["full_pipeline_duration_ms"])
            self.assertIsNone(report["cost_usd"])
            self.assertNotIn("PRIVATE BODY", json.dumps(report))

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "absent.sqlite3"
            with self.assertRaises(sqlite3.OperationalError):
                evaluation.read_production_telemetry(database, Path(directory), "2026-09-19T00:00:00Z")
            self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
