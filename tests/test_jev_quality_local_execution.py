"""Scope controls for retained local test output and invented verification gaps."""
import json
from pathlib import Path
import unittest

from codex_mem import jev_quality, processor


FIXTURE = Path(__file__).parent / 'fixtures/jev_quality_local_execution.json'


class LocalExecutionScopeTests(unittest.TestCase):
    def test_fixed_controls_distinguish_local_execution_from_other_sources(self):
        cases = json.loads(FIXTURE.read_text())["cases"]
        self.assertEqual(7, len(cases))
        self.assertEqual(2, sum(case["expected"] == "accept" for case in cases))
        self.assertEqual({"assistant_report_as_execution", "printed_claim_is_not_test",
                          "test_source_is_not_execution", "local_is_not_production",
                          "invented_evidence_gap"},
                         {case["category"] for case in cases if case["expected"] == "reject"})
        source = cases[0]["sources"][0]
        self.assertIsInstance(source["tool_io"]["response"], str)
        self.assertNotIn("exit_code", json.dumps(source))
        self.assertNotIn("assert duplicate_rejected", json.dumps(source))
        self.assertEqual("tool_record", processor._evidence_role(source))

    def test_generator_and_correction_preserve_scoped_test_results(self):
        source = dict(json.loads(FIXTURE.read_text())["cases"][0]["sources"][0], title="Local test")
        prompt = processor._build_prompt([source])
        self.assertIn("no second independent verifier is required", prompt)
        self.assertIn("Do not invent an omitted exit status or unseen assertions", prompt)
        self.assertIn("Echo/printf of a success claim", prompt)
        correction = processor._quality_correction_prompt({"decisions": []})
        self.assertIn("Preserve supported findings and observed local test outcomes", correction)
        self.assertIn("unverified production behavior or unseen implementation details", correction)

    def test_gate_caveats_cannot_deny_observed_local_results(self):
        self.assertIn("Missing-evidence caveats must match the supplied evidence", jev_quality._EVIDENCE_RULES)
        self.assertIn("without a second independent verifier", jev_quality._EVIDENCE_RULES)
        self.assertIn("an omitted exit status or unseen test assertions", jev_quality._EVIDENCE_RULES)
        self.assertIn("echo/printf", jev_quality._EVIDENCE_RULES)
        self.assertEqual(.8, jev_quality.ACCEPT_PROBABILITY)
        self.assertEqual(.2, jev_quality.REJECT_PROBABILITY)


if __name__ == '__main__':
    unittest.main()
