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

    def test_read_only_phases_do_not_reserve_model_budget(self):
        self.assertFalse(driver.needs_model("clone"))
        self.assertFalse(driver.needs_model("consolidate"))
        self.assertFalse(driver.needs_model("report"))
        self.assertTrue(all(driver.needs_model(phase) for phase in ("generate", "read", "judge")))


if __name__ == "__main__":
    unittest.main()
