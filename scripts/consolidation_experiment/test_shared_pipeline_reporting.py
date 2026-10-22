"""Public report checks for shared generation failures in a future round."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location(
    "shared_pipeline_experiment", ROOT / "scripts/project_memory_consolidation_experiment.py"
)
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


class SharedPipelineReportingTests(unittest.TestCase):
    def write_complete_answers(self, run, *, generation_status="complete"):
        rows = [{"id": f"q{index}", "full_supported_answer": True,
                 "current_state_actionable": True,
                 "constraints_obligations_reasons_recalled": True,
                 "citations_supported": True, "critical_errors": [],
                 "empty_or_evasive": False, "fact_recall": [], "category": "state"}
                for index in range(6)]
        for arm in ("baseline", "candidate"):
            directory = run / arm / "fictional-task"
            experiment.write(directory / "generation.json", {"status": generation_status})
            experiment.write(directory / "judge-0.json", {"grades": rows})
            experiment.write(directory / "contexts.json", [{"context_chars": 100}])
        experiment.write(run / "candidate/fictional-task/consolidation.json", {
            "status": "complete", "jobs": [], "accepted_merge_count": 0,
        })

    def shared_report(self, run, **settings):
        experiment.write(run / "run.json", {"budget_run": str(run)})
        return experiment.aggregate(
            types.SimpleNamespace(run=run),
            {"split": "holdout", "histories": [{"id": "fictional-task"}]},
            {"regeneration_repeat_history_ids": [],
             "pipeline_receipt_contract": "complete-shared-pipeline-v1", **settings},
        )

    def test_complete_shared_generation_receipt_is_counted_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.write_complete_answers(run)
            report = self.shared_report(run)
        self.assertEqual(report["generation_receipts"], [{"variant": "baseline",
            "history_id": "fictional-task", "stage": "generation", "status": "complete"}])
        self.assertEqual(report["pipeline_failures"], [])
        self.assertEqual(report["pilot_gate"], "pass")
        self.assertEqual(report["decision"], "no_improvement")
        self.assertEqual(report["usage"]["attempted_calls"], 0)

    def test_failed_shared_generation_cannot_admit_stale_perfect_grades(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.write_complete_answers(run, generation_status="failed")
            report = self.shared_report(run)
            protocol = {"regeneration_repeat_history_ids": []}
            legacy = experiment.aggregate(types.SimpleNamespace(run=run),
                {"split": "holdout", "histories": [{"id": "fictional-task"}]}, protocol)
        self.assertEqual(report["decision"], "incomplete")
        self.assertEqual(len(report["generation_receipts"]), 1)
        self.assertEqual(len(report["pipeline_failures"]), 1)
        self.assertTrue(all(row["questions_graded"] == 0 for row in report["variants"].values()))
        self.assertNotIn("pipeline_receipt_contract", legacy)
        self.assertEqual(legacy["generation_receipts"], [])

    def test_failed_physical_prefix_blocks_a_stale_successful_clone(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.write_complete_answers(run)
            experiment.write(run / "baseline/fictional-task/generation.json", {"status": "failed"})
            report = self.shared_report(run)
        self.assertTrue(all(row["questions_graded"] == 0 for row in report["variants"].values()))
        self.assertEqual(report["decision"], "incomplete")

    def test_failed_regeneration_does_not_admit_stale_repeat_grades(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.write_complete_answers(run)
            repeat = run / "replicates/regeneration-1"
            self.write_complete_answers(repeat)
            experiment.write(repeat / "baseline/fictional-task/generation.json", {"status": "failed"})
            report = self.shared_report(run, regeneration_repeat_history_ids=["fictional-task"])
        self.assertEqual(report["regeneration_repeat_results"], [])
        self.assertFalse(report["repeats_complete"])
        self.assertEqual(report["decision"], "incomplete")

    def test_failed_candidate_preparation_identifies_its_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            self.write_complete_answers(run)
            experiment.write(run / "candidate/fictional-task/consolidation.json", {
                "status": "failed", "jobs": [], "accepted_merge_count": 0,
            })
            report = self.shared_report(run)
        self.assertEqual(report["decision"], "fail")
        self.assertEqual(len(report["pipeline_failures"]), 1)
        failure = report["pipeline_failures"][0]
        self.assertEqual(failure.get("variant"), "candidate")
        self.assertEqual(failure.get("stage"), "consolidation")

    def test_invalid_contract_rejects_before_run_metadata_creation(self):
        for value in (None, False, "other"):
            with self.subTest(value=value), tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
                root = Path(temporary)
                protocol = root / "protocol.json"
                experiment.write(protocol, {"pipeline_receipt_contract": value})
                run = root / "run"
                argv = ["experiment", "validate", "--run", str(run), "--protocol", str(protocol)]
                with patch.object(sys, "argv", argv), self.assertRaises(ValueError):
                    experiment.main()
                self.assertFalse(run.exists())
        with self.assertRaises(ValueError):
            experiment.aggregate(types.SimpleNamespace(run=Path("/unused")), {}, {
                "comparison_mode": "independent-generation",
                "pipeline_receipt_contract": "complete-shared-pipeline-v1",
            })

    def test_required_missing_regeneration_stages_remain_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            experiment.write(run / "run.json", {"budget_run": str(run)})
            report = experiment.aggregate(
                types.SimpleNamespace(run=run),
                {"split": "holdout", "histories": [{"id": "fictional-task"}]},
                {"regeneration_repeat_history_ids": ["fictional-task"],
                 "pipeline_receipt_contract": "complete-shared-pipeline-v1"},
            )
        stages = {(row["variant"], row["stage"], row["status"])
                  for row in report["regeneration_pipeline_receipts"]}
        self.assertEqual(stages, {("baseline", "generation", "missing"),
                                  ("candidate", "consolidation", "missing")})
        self.assertEqual(len(report["regeneration_pipeline_receipts"]), 2)
        self.assertEqual(report["decision"], "incomplete")

    def test_failed_shared_generation_is_reported_with_full_missing_denominator(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            experiment.write(run / "run.json", {"budget_run": str(run)})
            experiment.write(run / "baseline/fictional-task/generation.json", {
                "status": "failed", "jobs": [{"status": "failed", "code": "invalid_response"}]
            })
            report = experiment.aggregate(
                types.SimpleNamespace(run=run),
                {"split": "holdout", "histories": [{"id": "fictional-task"}]},
                {"regeneration_repeat_history_ids": [],
                 "pipeline_receipt_contract": "complete-shared-pipeline-v1"},
            )
        self.assertEqual(report["decision"], "incomplete")
        for arm in ("baseline", "candidate"):
            self.assertEqual(report["variants"][arm]["questions_planned"], 6)
            self.assertEqual(report["variants"][arm]["questions_graded"], 0)
        self.assertTrue(any(
            row.get("stage") == "generation" and row.get("variant") == "baseline"
            and row.get("status") == "failed" and row.get("history_id") == "fictional-task"
            for row in report["pipeline_failures"]
        ))


if __name__ == "__main__":
    unittest.main()
