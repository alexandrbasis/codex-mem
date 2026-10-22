"""Check the new driver's dispatch order without opening fixtures or calling models."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import unittest


spec = spec_from_file_location("paired_driver", Path(__file__).with_name("run-experiment.py"))
driver = module_from_spec(spec)
spec.loader.exec_module(driver)


class SharedPrefixPlanTest(unittest.TestCase):
    def test_primary_and_regeneration_use_one_generation_per_pair(self):
        plan = list(driver.phase_plan(["dev_101", "dev_102"], ["dev_101"]))
        self.assertEqual(sum(row[0] == "generate" for row in plan), 3)
        self.assertTrue(all(row[2] == "baseline" for row in plan if row[0] == "generate"))
        for history, replicate in (("dev_101", None), ("dev_102", None),
                                   ("dev_101", "regeneration-1")):
            pair = [row for row in plan if row[1] == history and row[3] == replicate]
            self.assertEqual([row[0] for row in pair[:3]], ["generate", "clone", "consolidate"])
            self.assertEqual([(row[0], row[2]) for row in pair[3:]],
                             [("read", "baseline"), ("read", "candidate"),
                              ("judge", "baseline"), ("judge", "candidate")])

    def test_default_runtime_selection_preserves_legacy_generation_and_readers(self):
        baseline, repo = Path("/tmp/control-runtime"), Path("/tmp/current-repo")
        for protocol in ({}, {"comparison_mode": "shared-prefix", "shared_generation_runtime": "baseline"}):
            with self.subTest(protocol=protocol):
                self.assertEqual(baseline, driver.phase_runtime("generate", "baseline", protocol, baseline, repo))
                self.assertEqual(baseline, driver.phase_runtime("read", "baseline", protocol, baseline, repo))
                self.assertEqual(repo, driver.phase_runtime("read", "candidate", protocol, baseline, repo))
                self.assertEqual(repo, driver.phase_runtime("consolidate", "candidate", protocol, baseline, repo))

    def test_candidate_prefix_uses_repo_generation_but_does_not_change_baseline_reader(self):
        baseline, repo = Path("/private/tmp/control-runtime"), Path("/private/tmp/current-repo")
        protocol = {"comparison_mode": "shared-prefix", "shared_generation_runtime": "candidate",
                    "baseline_runtime_root": str(baseline.resolve())}
        plan = list(driver.phase_plan(["hold_901", "hold_902"], ["hold_901"]))
        for phase, _, variant, _ in plan:
            with self.subTest(phase=phase, variant=variant):
                runtime = driver.phase_runtime(phase, variant, protocol, baseline, repo)
                expected = repo if phase == "generate" or variant == "candidate" else baseline
                self.assertEqual(expected, runtime)
                if phase == "generate":
                    self.assertEqual("baseline", variant)
        self.assertEqual(3, sum(phase == "generate" for phase, _, _, _ in plan))
        self.assertEqual(repo, driver.phase_runtime("report", None, protocol, baseline, repo))

    def test_candidate_prefix_refuses_undeclared_or_wrong_baseline_runtime(self):
        baseline, repo = Path("/private/tmp/control-runtime"), Path("/private/tmp/current-repo")
        for declared in (None, "/private/tmp/wrong-control"):
            protocol = {"comparison_mode": "shared-prefix", "shared_generation_runtime": "candidate",
                        "baseline_runtime_root": declared}
            with self.subTest(declared=declared), self.assertRaisesRegex(ValueError, "baseline_runtime_differs"):
                driver.phase_runtime("generate", "baseline", protocol, baseline, repo)
        for protocol in ({"shared_generation_runtime": "other"},
                         {"comparison_mode": "independent-generation", "shared_generation_runtime": "candidate"}):
            with self.assertRaisesRegex(ValueError, "invalid_shared_generation_runtime"):
                driver.phase_runtime("generate", "baseline", protocol, baseline, repo)

    def test_read_only_phases_do_not_reserve_model_budget(self):
        self.assertFalse(driver.needs_model("clone"))
        self.assertFalse(driver.needs_model("consolidate"))
        self.assertFalse(driver.needs_model("report"))
        self.assertTrue(all(driver.needs_model(phase) for phase in ("generate", "read", "judge")))


if __name__ == "__main__":
    unittest.main()
