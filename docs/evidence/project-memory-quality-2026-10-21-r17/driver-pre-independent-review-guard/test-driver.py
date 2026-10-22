"""Check the new driver's dispatch order without opening fixtures or calling models."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import unittest
import hashlib
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

_fixture_spec = spec_from_file_location("public_toy_contract_tests", Path(__file__).with_name("test_public_fixture_contract_v2.py"))
_fixture_tests = module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_tests)


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


class AuthorInputGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.fixture, self.gold = _fixture_tests.toy()
        self.fp, self.gp = self.run / 'fixture.json', self.run / 'gold.json'

    def write_inputs(self):
        self.fp.write_text(json.dumps(self.fixture))
        self.gp.write_text(json.dumps(self.gold))

    def test_valid_guard_receipt_binds_exact_inputs_and_contract(self):
        self.write_inputs()
        receipt = driver.validate_author_inputs(self.fp, self.gp, self.run, expected_contract_sha256=hashlib.sha256(
                    Path(driver.__file__).with_name('public-fixture-contract.py').read_bytes()).hexdigest())
        self.assertEqual(receipt, json.loads((self.run / 'author-input-validation.json').read_text()))
        for key, path in (('fixture_sha256', self.fp), ('gold_sha256', self.gp),
                          ('contract_sha256', Path(driver.__file__).with_name('public-fixture-contract.py'))):
            self.assertEqual(receipt[key], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(receipt['validation']['questions'], 96)
        self.assertEqual(receipt['validation']['status'], 'positive_source_anchors_representable')

    def test_conflicting_success_receipt_is_not_overwritten(self):
        self.write_inputs()
        receipt_path = self.run / 'author-input-validation.json'
        receipt_path.write_text('{"prior_validation":"preserve"}\n')
        original = receipt_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'receipt_differs'):
            driver.validate_author_inputs(self.fp, self.gp, self.run, expected_contract_sha256=hashlib.sha256(
                    Path(driver.__file__).with_name('public-fixture-contract.py').read_bytes()).hexdigest())
        self.assertEqual(receipt_path.read_bytes(), original)

    def test_changed_contract_bytes_are_rejected_before_import_or_execution(self):
        self.write_inputs()
        expected = hashlib.sha256(Path(driver.__file__).with_name('public-fixture-contract.py').read_bytes()).hexdigest()
        changed = self.run / 'public-fixture-contract.py'
        marker = self.run / 'contract-executed'
        changed.write_text('from pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("executed")\n')
        with patch.object(driver, '__file__', str(self.run / 'run-experiment.py')), \
             patch.object(driver.importlib.util, 'spec_from_file_location') as importing:
            with self.assertRaisesRegex(RuntimeError, 'author_contract_differs_from_protocol'):
                driver.validate_author_inputs(self.fp, self.gp, self.run, expected_contract_sha256=expected)
        importing.assert_not_called()
        self.assertFalse(marker.exists())
        self.assertFalse((self.run / 'author-input-validation.json').exists())

    def test_actual_guard_rejects_invalid_question_or_delete_positive_without_receipt(self):
        for failure in ('question', 'delete_positive'):
            with self.subTest(failure=failure):
                self.fixture, self.gold = _fixture_tests.toy()
                if failure == 'question':
                    del self.fixture['histories'][0]['questions'][0]['question']
                else:
                    self.gold['questions']['toy_0/q0']['required_facts'][0]['support'][0].update(
                        event_id='delete', start=0, end=20, quote='Remove the old note.')
                self.write_inputs()
                original = self.fp.read_bytes(), self.gp.read_bytes()
                with self.assertRaises(ValueError):
                    driver.validate_author_inputs(self.fp, self.gp, self.run, expected_contract_sha256=hashlib.sha256(
                    Path(driver.__file__).with_name('public-fixture-contract.py').read_bytes()).hexdigest())
                self.assertFalse((self.run / 'author-input-validation.json').exists())
                self.assertEqual(original, (self.fp.read_bytes(), self.gp.read_bytes()))

    def test_main_validates_pins_then_actual_author_contract_before_fixture_journal_or_dispatch(self):
        del self.fixture['histories'][0]['questions'][0]['question']
        self.write_inputs()
        events = []
        helper = SimpleNamespace()
        helper.read = Mock(return_value={'driver_sha256': hashlib.sha256(Path(driver.__file__).read_bytes()).hexdigest(),
            'comparison_mode': 'shared-prefix', 'candidate_processing_mode': 'read_only_snapshot',
            'prospective_author_contract': {'sha256': hashlib.sha256(
                Path(driver.__file__).with_name('public-fixture-contract.py').read_bytes()).hexdigest()}})
        helper.fixture = Mock(side_effect=AssertionError('fixture_opened_before_author_validation'))
        helper.append = Mock(side_effect=AssertionError('journal_written_before_author_validation'))
        helper.ledger_calls = Mock(side_effect=AssertionError('phase_started_before_author_validation'))
        original_spec, original_module = driver.importlib.util.spec_from_file_location, driver.importlib.util.module_from_spec
        helper_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))
        def find_spec(name, path):
            return helper_spec if name == 'experiment' else original_spec(name, path)
        def make_module(spec):
            return helper if spec is helper_spec else original_module(spec)
        def validate_pins(command, **kwargs):
            self.assertEqual(command[2], 'validate')
            events.append('frozen_pins_validated')
        actual_guard = driver.validate_author_inputs
        def guard(*args, **kwargs):
            self.assertEqual(events, ['frozen_pins_validated'])
            events.append('author_contract')
            return actual_guard(*args, **kwargs)
        argv = ['driver', '--run', str(self.run), '--baseline', '/private/tmp/public-baseline',
                '--fixture', str(self.fp), '--gold', str(self.gp)]
        with patch.object(driver.sys, 'argv', argv), patch.dict(driver.os.environ, {'CODEX_MEM_DISABLED': '0'}), \
             patch.object(driver.importlib.util, 'spec_from_file_location', side_effect=find_spec), \
             patch.object(driver.importlib.util, 'module_from_spec', side_effect=make_module), \
             patch.object(driver.subprocess, 'run', side_effect=validate_pins) as dispatch, \
             patch.object(driver, 'validate_author_inputs', side_effect=guard):
            with self.assertRaises(ValueError):
                driver.main()
        self.assertEqual(events, ['frozen_pins_validated', 'author_contract'])
        self.assertEqual(dispatch.call_count, 1)
        helper.fixture.assert_not_called()
        helper.append.assert_not_called()
        helper.ledger_calls.assert_not_called()
        self.assertFalse((self.run / 'baseline').exists())
        self.assertFalse((self.run / 'author-input-validation.json').exists())


if __name__ == "__main__":
    unittest.main()
