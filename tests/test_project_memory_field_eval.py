"""Offline tests for experiment mechanics, never model-quality assertions."""
import importlib.util
import json
from pathlib import Path

import tempfile
import unittest

PATH = Path(__file__).resolve().parents[1] / 'scripts/project_memory_field_eval.py'
spec = importlib.util.spec_from_file_location('field_eval', PATH)
evalmod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evalmod)


def response(payload, probability=.95, input_tokens=10):
    return {'model': payload['model'], 'answers': {
        name: {'type': 'noul', 'noul': .05 if name.endswith('overclaim') else probability}
        for name in payload['questions']},
        'usage': {'input_tokens': input_tokens, 'output_tokens': len(payload['questions'])}}


class FieldEvalTests(unittest.TestCase):
    def test_fixture_is_balanced_final_assembly_and_hides_labels(self):
        cases = evalmod.load_cases()
        assert len(cases) == 24
        assert sum(c['label']['purpose'] for c in cases) == 12
        for case in cases:
            data = evalmod.public_case(case)
            assert not {'label', 'category', 'id'} & data.keys()
            assert case['final_value'] == data['final_value']
            notes, claimed = evalmod.baseline_input(case)
            items = evalmod.jev_quality._items(notes, None, claimed)
            assert items[0]['field_evidence'][0]['quote'] == case['evidence'][0]['quote']
        assert {c['category'] for c in cases} >= {'cli_help', 'example', 'quotation', 'assistant_report',
            'explicit_obligation', 'scoped_execution', 'compound_cancellation', 'adversarial_quote'}


    def test_bad_final_assembly_fails_before_dispatch(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        raw = json.loads(evalmod.FIXTURE.read_text())
        raw['cases'][0]['final_value'] += ' invented'
        path = tmp_path / 'cases.json'
        path.write_text(json.dumps(raw))
        with self.assertRaisesRegex(ValueError, 'final assembled'):
            evalmod.load_cases(path)


    def test_budget_counts_failed_attempts_and_stops_unknown_usage(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        def fail(payload):
            raise TimeoutError('never persist this sensitive text')
        ledger = evalmod.Ledger(tmp_path / 'ledger.json', fail)
        payload = evalmod.purpose_payload(evalmod.load_cases()[:1])
        with self.assertRaises(TimeoutError):
            ledger(payload)
        with self.assertRaises(evalmod.BudgetStop):
            ledger(payload)
        assert len(ledger.rows) == 1
        assert ledger.rows[0]['status'] == 'transport_failed'
        assert 'sensitive' not in ledger.path.read_text()
        with self.assertRaises(FileExistsError):
            evalmod.Ledger(ledger.path, fail)


    def test_exact_attempt_and_input_budget(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        payload = evalmod.purpose_payload(evalmod.load_cases()[:1])
        ledger = evalmod.Ledger(tmp_path / 'ledger.json', response, max_requests=1)
        ledger(payload)
        with self.assertRaises(evalmod.BudgetStop):
            ledger(payload)
        tiny = evalmod.Ledger(tmp_path / 'tiny.json', response, max_input_tokens=100)
        with self.assertRaises(evalmod.BudgetStop):
            tiny(payload)
        assert not tiny.rows


    def test_invalid_response_preserves_reported_usage(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        def invalid(payload):
            return dict(response(payload, input_tokens=17), answers={})
        ledger = evalmod.Ledger(tmp_path / 'ledger.json', invalid)
        with self.assertRaises(evalmod.jev_client.JevError):
            ledger(evalmod.purpose_payload(evalmod.load_cases()[:1]))
        assert ledger.rows[0]['usage']['input_tokens'] == 17
        assert ledger.rows[0]['status'] == 'invalid_response'


    def test_full_run_alignment_usage_and_all_planned_denominator(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        cases = evalmod.load_cases()
        # The deterministic evaluator assigns probabilities by batch position.
        # This checks alignment only; it does not reproduce the gold-label policy.
        def alternating(payload):
            value = response(payload)
            for name in value['answers']:
                if name.startswith('record_'):
                    value['answers'][name]['noul'] = .95 if int(name.split('_')[1]) % 2 == 0 else .05
            return value
        ledger = evalmod.Ledger(tmp_path / 'ledger.json', alternating)
        report = evalmod.run(cases, ledger)
        assert report['status'] == 'complete'
        assert report['accounting']['attempts'] == 30
        assert report['accounting']['usage']['input_tokens'] == 300
        assert all(report['results'][c['id']]['purpose'] == ('accept' if i % 2 == 0 else 'rejected')
                   for i, c in enumerate(cases))
        assert report['metrics']['baseline']['false_accept'] == 12
        assert report['metrics']['combined']['false_accept'] == 0
        assert report['metrics']['combined']['useful_loss'] == 0


    def test_partial_run_keeps_all_planned_cases(self):
        tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))
        cases = evalmod.load_cases()
        ledger = evalmod.Ledger(tmp_path / 'ledger.json', response, max_requests=1)
        report = evalmod.run(cases, ledger)
        assert report['status'] == 'incomplete'
        assert report['accounting']['attempts'] == 1
        assert report['metrics']['baseline']['planned'] == 24
        assert report['metrics']['baseline']['useful_loss'] == 12
        assert report['metrics']['baseline']['unresolved'] == 24
        assert report['metrics']['purpose']['unresolved'] == 20


    def test_thresholds_preserve_uncertainty(self):
        assert evalmod.route(.8) == 'accept'
        assert evalmod.route(.2) == 'rejected'
        assert evalmod.route(.5) == 'uncertain'


if __name__ == "__main__":
    unittest.main()
