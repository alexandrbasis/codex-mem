"""Prospective canonical history selection, with legacy accounting preserved."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/'project_memory_consolidation_experiment.py'
spec=importlib.util.spec_from_file_location('historical_usage_experiment',SCRIPT)
experiment=importlib.util.module_from_spec(spec);spec.loader.exec_module(experiment)


def usage(total,unknown=False):
    rows=[{'call_id':'known','status':'started','model':'gpt-6-luna'},
          {'call_id':'known','status':'completed','metrics':{'usage':{'status':'reported','tokens':{
              'input_tokens':total-2,'cached_input_tokens':0,'cache_write_input_tokens':0,
              'output_tokens':2,'reasoning_output_tokens':0,'total_tokens':total}}}}]
    if unknown:rows += [{'call_id':'unknown','status':'started'}, {'call_id':'unknown','status':'failed'}]
    return experiment.usage(rows)


class HistoricalUsageSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.report=Path(self.temp.name)/'canonical-report.json'
        experiment.write(self.report,{'usage':usage(12),'cumulative_pilot_usage':usage(100,True)})
        self.pointer={'report':str(self.report),'sha256':hashlib.sha256(self.report.read_bytes()).hexdigest()}

    def total(self,pointer):
        return experiment.historical_accounting({'historical_spend':pointer},usage(20))['cumulative_pilot_usage']

    def test_cumulative_selector_carries_unknown_and_counts_prior_chain_once(self):
        result=self.total({**self.pointer,'usage_field':'cumulative_pilot_usage'})
        self.assertEqual(result['attempted_calls'],3)
        self.assertEqual(result['failed_calls'],1)
        self.assertEqual(result['reported_tokens_lower_bound']['total_tokens'],120)
        self.assertEqual(result['calls_with_unknown_or_partial_tokens'],1)
        self.assertIsNone(result['tokens']['total_tokens'])
        self.assertFalse(result['token_accounting_complete'])
        self.assertFalse(result['pricing_complete'])
        self.assertIsNone(result['cost_usd'])

    def test_absent_and_explicit_legacy_selector_match_existing_current_usage(self):
        absent=self.total(self.pointer)
        explicit=self.total({**self.pointer,'usage_field':'usage'})
        self.assertEqual(absent,explicit)
        self.assertEqual(absent['attempted_calls'],2)
        self.assertEqual(absent['tokens']['total_tokens'],32)

    def test_invalid_selector_and_missing_cumulative_do_not_fallback(self):
        for selector in ('', 'historical_usage',False,None,[],{}):
            with self.subTest(selector=selector),self.assertRaises(ValueError):
                self.total({**self.pointer,'usage_field':selector})
        experiment.write(self.report,{'usage':usage(12)})
        self.pointer['sha256']=hashlib.sha256(self.report.read_bytes()).hexdigest()
        with self.assertRaises(KeyError):self.total({**self.pointer,'usage_field':'cumulative_pilot_usage'})

    def test_selected_cumulative_report_still_requires_original_byte_pin(self):
        experiment.write(self.report,{'usage':usage(12),'cumulative_pilot_usage':usage(200,True)})
        with self.assertRaisesRegex(ValueError,'historical spend report changed'):
            self.total({**self.pointer,'usage_field':'cumulative_pilot_usage'})

if __name__=='__main__':unittest.main()
