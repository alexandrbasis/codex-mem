"""Report behavior tests use canned responses, never fixture-label classifiers."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_mem import jev_client

SPEC = importlib.util.spec_from_file_location("jev_integration_eval", Path(__file__).resolve().parents[1]
                                            / "scripts/jev_integration_eval.py")
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


def fixture_subset(quality=(0, 2), retrieval=(7,)):
    fixture = evaluation.load_fixture(evaluation.DEFAULT_FIXTURE)
    fixture["quality_cases"] = [fixture["quality_cases"][index] for index in quality]
    fixture["retrieval_cases"] = [fixture["retrieval_cases"][index] for index in retrieval]
    return fixture


def canned_response(payload, *, grounded=.99, overclaim=.01, useful=(0,), tokens=100):
    """Build typed envelopes from explicit canned values, without reading state."""
    answers = {}
    for name, question in payload["questions"].items():
        if name.endswith("_grounded"):
            answers[name] = {"type": "noul", "noul": grounded}
        elif name.endswith("_overclaim"):
            answers[name] = {"type": "noul", "noul": overclaim}
        elif name == "current_state":
            answers[name] = {"type": "noul", "noul": .01}
        elif name.startswith("relevant_"):
            answers[name] = {"type": "noul", "noul": .99 if int(name.split("_")[1]) in useful else .01}
        else:
            score = 3 if int(name.split("_")[1]) in useful else 0
            answers[name] = {"type": "score", "score": score, "confidence": .99,
                             "probabilities": {str(i): float(i == score) for i in range(4)},
                             "legend": {str(i): text for i, text in enumerate(question["criteria"])}}
    return {"model": jev_client.MODEL, "answers": answers,
            "usage": {"input_tokens": tokens, "output_tokens": 5}}


class JevIntegrationEvaluationTests(unittest.TestCase):
    def test_fixture_labels_are_fixed_and_bilingual_with_required_evidence_cases(self):
        fixture = evaluation.load_fixture(evaluation.DEFAULT_FIXTURE)
        self.assertEqual((30, 8), (len(fixture["quality_cases"]), len(fixture["retrieval_cases"])))
        self.assertEqual({"en", "ru"}, {case["language"] for case in fixture["quality_cases"]})
        self.assertEqual({"en", "ru"}, {case["language"] for case in fixture["retrieval_cases"]})
        self.assertEqual(15, sum(case["critical_unsupported"] for case in fixture["quality_cases"]))
        self.assertTrue({"assistant_as_verified", "plan_as_completed", "numeric_mismatch", "scope_expansion",
                         "conflict_erased", "injection_followed", "short_useful_finding"}
                        <= {case["category"] for case in fixture["quality_cases"]})
        self.assertIn("before any live evaluation", fixture["labeling"]["method"])

    def test_labels_are_excluded_and_real_cache_counts_each_request_once(self):
        fixture = fixture_subset()
        calls = []

        def respond(payload):
            state = json.dumps(payload["state"])
            for label in ('"expected"', '"critical_unsupported"', '"reason"', '"relevant_ids"', '"category"'):
                self.assertNotIn(label, state)
            calls.append(payload)
            # Fixed first response accepts, second rejects, third ranks only c0.
            return canned_response(payload, grounded=.01 if len(calls) == 2 else .99,
                                   overclaim=.99 if len(calls) == 2 else .01, tokens=200)

        report = evaluation.evaluate_fixture(fixture, evaluator=respond)
        self.assertEqual("completed", report["status"])
        self.assertEqual("test_double", report["evidence_mode"])
        self.assertEqual(3, len(calls))
        self.assertEqual(["accept", "reject"], [r["actual"] for r in report["passes"][0]["quality"]])
        self.assertEqual(3, report["cache_comparison"]["warm_cache_hits"])
        self.assertTrue(report["cache_comparison"]["warm_zero_requests_and_tokens"])
        self.assertTrue(report["cache_comparison"]["decisions_unchanged"])
        self.assertEqual(600, report["cumulative_metrics"]["input_tokens"])
        self.assertEqual(3, report["cumulative_metrics"]["requests"])
        self.assertEqual(6, report["cumulative_metrics"]["evaluation_receipts"])
        self.assertEqual("0.0000252", report["cumulative_metrics"]["input_cost_usd"]["complete_total"])
        self.assertEqual(evaluation.digest(fixture), report["fixture_sha256"])
        self.assertTrue(all(len(policy["module_sha256"]) == 64 for policy in report["policies"].values()))
        self.assertFalse(report["generator"]["executed"])
        self.assertIsNone(report["generator"]["saved_tokens"])
        self.assertFalse(report["acceptance"]["passed"])
        self.assertIn("test_double_is_not_live_quality_evidence", report["acceptance"]["blocking_reasons"])

    def test_completed_execution_does_not_pass_an_unsupported_claim(self):
        report = evaluation.evaluate_fixture(fixture_subset(), evaluator=canned_response)
        self.assertEqual("completed", report["status"])
        self.assertEqual(1, report["passes"][0]["quality_scores"]["critical_unsupported_accepted"])
        self.assertFalse(report["acceptance"]["passed"])
        self.assertIn("cold:critical_unsupported_claim_accepted", report["acceptance"]["blocking_reasons"])

    def test_uncertain_quality_is_unresolved_and_counts_against_useful_recall(self):
        report = evaluation.evaluate_fixture(fixture_subset(), evaluator=lambda payload:
            canned_response(payload, grounded=.5, overclaim=.5))
        score = report["passes"][0]["quality_scores"]
        self.assertEqual(2, score["unresolved"])
        self.assertEqual(0, score["useful_recall"])
        self.assertFalse(report["acceptance"]["passed"])
        self.assertIn("cold:useful_quality_recall_regressed", report["acceptance"]["blocking_reasons"])

    def test_api_failures_are_not_zero_cost_or_successful_retrieval(self):
        def fail(_payload):
            raise jev_client.JevError("jev_transport")

        report = evaluation.evaluate_fixture(fixture_subset(), evaluator=fail)
        self.assertEqual("completed", report["status"])
        self.assertEqual("unresolved", report["passes"][0]["retrieval"][0]["resolution"])
        self.assertEqual(6, report["cumulative_metrics"]["requests"])
        self.assertIsNone(report["cumulative_metrics"]["input_tokens"])
        self.assertIsNone(report["cumulative_metrics"]["input_cost_usd"]["complete_total"])
        self.assertFalse(report["acceptance"]["passed"])

    def test_paid_usage_survives_invalid_response_and_cache_is_not_claimed(self):
        report = evaluation.evaluate_fixture(fixture_subset(), evaluator=lambda _payload: {
            "model": "wrong", "usage": {"input_tokens": 321, "output_tokens": 4}})
        self.assertEqual(6 * 321, report["cumulative_metrics"]["input_tokens_reported_or_partial"])
        self.assertIsNone(report["cumulative_metrics"]["input_tokens"])
        self.assertFalse(report["cache_comparison"]["warm_zero_requests_and_tokens"])
        self.assertIsNone(report["cumulative_metrics"]["input_cost_usd"]["complete_total"])

    def test_retrieval_regression_and_no_match_have_separate_metrics(self):
        fixture = evaluation.load_fixture(evaluation.DEFAULT_FIXTURE)
        row = evaluation.retrieval_score(fixture["retrieval_cases"][6], ["r07c0", "r07c2"])
        self.assertTrue(row["recall_regressed"])
        self.assertEqual(.5, row["recall"])
        no_match = evaluation.retrieval_score(fixture["retrieval_cases"][2], ["r03c0"])
        self.assertFalse(no_match["no_match_correct"])
        self.assertEqual(["r03c0"], no_match["forbidden_results"])
        self.assertIsNone(no_match["recall"])

    def test_acceptance_rejects_regressions_even_with_other_improved_queries(self):
        report = evaluation.evaluate_fixture(fixture_subset(), evaluator=canned_response)
        for phase in report["passes"]:
            phase["quality_scores"].update(critical_unsupported_accepted=0, useful_recall=1, unresolved=0)
            phase["retrieval"][0].update(recall_improved=True, recall_regressed=True)
        report["evidence_mode"] = "live_api"
        report["cache_comparison"].update(measured_gate_wall_time_reduced=True, reported_input_cost_reduced=True)
        result = evaluation.acceptance(report)
        self.assertFalse(result["passed"])
        self.assertIn("cold:retrieval_recall_regressed", result["blocking_reasons"])

    def test_missing_usage_or_missing_receipts_remain_unknown(self):
        audit = {"model": jev_client.MODEL, "counts": {"requests": 1, "cache_hits": 0},
                 "usage_status": "reported", "usage": {"input_tokens": 100, "output_tokens": 0}, "duration_ms": 2}
        result = evaluation.metrics([{"audit": {"evaluations": [audit]}, "wall_ms": 3}, {"audit": {}, "wall_ms": 1}])
        self.assertEqual(1, result["evaluation_receipts"])
        self.assertEqual(1, result["gate_calls_without_evaluation_receipt"])
        self.assertIsNone(result["input_tokens"])
        self.assertIsNone(result["input_cost_usd"]["complete_total"])

    def test_intentional_complete_protected_noop_is_resolved_without_invented_api_audit(self):
        case = evaluation.load_fixture(evaluation.DEFAULT_FIXTURE)["retrieval_cases"][4]
        score = evaluation.retrieval_score(case, ["r05c0"])
        audit = {"status": "skipped", "skip_reason": "protected_no_additions",
                 "requests": 0, "cache_hits": 0, "evaluated_candidates": 0}
        resolution, source = evaluation.retrieval_resolution(case, score, audit)
        self.assertEqual(("resolved", "local_protected_noop"), (resolution, source))
        totals = evaluation.metrics([{"audit": audit, "decision_source": source, "wall_ms": .3}])
        self.assertEqual(0, totals["evaluation_receipts"])
        self.assertEqual(1, totals["verified_local_noop_calls"])
        self.assertEqual(0, totals["requests"])
        self.assertEqual(0, totals["input_tokens"])
        self.assertEqual("0", totals["input_cost_usd"]["complete_total"])
        for changes in ({"skip_reason": "disabled"}, {"requests": 1}, {"status": "fallback_error"}):
            self.assertEqual("unresolved", evaluation.retrieval_resolution(case, score, {**audit, **changes})[0])

    def test_default_cli_is_network_free_and_unresolved_exits_one(self):
        with mock.patch.object(evaluation, "evaluate_fixture") as execute, \
                mock.patch.object(evaluation, "load_config") as config, contextlib.redirect_stdout(io.StringIO()) as output:
            code = evaluation.main([])
        execute.assert_not_called()
        config.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertEqual(1, code)
        self.assertEqual("not_run", report["status"])
        self.assertFalse(report["acceptance"]["passed"])

    def test_cli_reads_config_only_when_explicitly_requested_and_exits_on_acceptance(self):
        for configured in (False, True):
            for accepted in (False, True):
                report = {"status": "completed", "acceptance": {"passed": accepted}}
                with mock.patch.object(evaluation, "evaluate_fixture", return_value=report) as execute, \
                        mock.patch.object(evaluation, "load_config", return_value={"jev_filter_key_file": "configured.key"}) as config, \
                        contextlib.redirect_stdout(io.StringIO()):
                    code = evaluation.main(["--live"] + (["--configured-key"] if configured else []))
                self.assertEqual(0 if accepted else 1, code)
                self.assertEqual(1 if configured else 0, config.call_count)
                self.assertEqual("configured.key" if configured else "", execute.call_args.kwargs["key_file"])

    def test_fixture_rejects_false_critical_labels_and_cross_case_candidates(self):
        fixture = fixture_subset()
        fixture["quality_cases"][1]["critical_unsupported"] = False
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.json"
            path.write_text(json.dumps(fixture))
            with self.assertRaisesRegex(ValueError, "invalid_quality_evidence"):
                evaluation.load_fixture(path)
            fixture = fixture_subset()
            fixture["retrieval_cases"][0]["relevant_ids"].append("foreign")
            path.write_text(json.dumps(fixture))
            with self.assertRaisesRegex(ValueError, "invalid_retrieval_labels"):
                evaluation.load_fixture(path)


if __name__ == "__main__":
    unittest.main()
