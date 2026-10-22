"""Public report checks for shared generation failures in a future round."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location(
    "shared_pipeline_experiment", ROOT / "scripts/project_memory_consolidation_experiment.py"
)
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


class SharedPipelineReportingTests(unittest.TestCase):
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
