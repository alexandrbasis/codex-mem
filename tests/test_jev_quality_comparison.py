"""The synthetic pilot tests plumbing; live receipts establish model behavior."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

from codex_mem import jev_quality

SPEC = importlib.util.spec_from_file_location(
    "jev_quality_comparison", Path(__file__).resolve().parents[1] / "scripts/jev_quality_comparison.py")
comparison = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(comparison)


class JevQualityComparisonTests(unittest.TestCase):
    def test_fixture_covers_scoped_support_and_four_unsupported_controls(self):
        fixture = json.loads(comparison.FIXTURE.read_text())
        cases = fixture["cases"]
        self.assertEqual(8, len(cases))
        self.assertEqual(4, sum(case["expected"] == "accept" for case in cases))
        self.assertEqual({"assistant_report_as_tool_proof", "omitted_result", "attribution_omitted",
                          "injected_completion"},
                         {case["category"] for case in cases if case["expected"] == "reject"})

    def test_labels_do_not_enter_state_and_receipts_do_not_copy_evidence(self):
        fixture = json.loads(comparison.FIXTURE.read_text())
        original = copy.deepcopy(fixture)
        calls = []

        def evaluator(payload):
            calls.append(payload)
            state = json.dumps(payload["state"])
            self.assertNotIn('"expected"', state)
            self.assertNotIn('"category"', state)
            return {"model": jev_quality.MODEL, "answers": {
                key: {"type": "noul", "noul": .99 if key.endswith("grounded") else .01}
                for key in payload["questions"]}, "usage": {"input_tokens": 100, "output_tokens": 5}}

        report = comparison.compare(jev_quality, fixture, evaluator=evaluator)
        self.assertEqual("test_double", report["evidence_mode"])
        self.assertEqual(16, len(calls))
        self.assertEqual(original, fixture)
        for phase in report["passes"]:
            self.assertEqual(4, phase["unsupported_accepted"])
            self.assertEqual(800, phase["input_tokens"])
        for case in fixture["cases"]:
            self.assertNotIn(case["candidate"]["body"], json.dumps(report))

    def test_request_budget_blocks_additional_dispatch_and_records_unknown_usage(self):
        fixture = json.loads(comparison.FIXTURE.read_text())
        evaluator = mock.Mock(side_effect=RuntimeError("not a provider receipt"))
        with mock.patch.object(comparison, "MAX_REQUESTS", 1):
            report = comparison.compare(jev_quality, fixture, evaluator=evaluator)
        self.assertEqual(1, evaluator.call_count)
        self.assertEqual(1, report["dispatched_requests"])
        self.assertTrue(all(not phase["usage_complete"] for phase in report["passes"]))
        self.assertTrue(all(phase["supported_accepted"] == 0 for phase in report["passes"]))


if __name__ == "__main__":
    unittest.main()
