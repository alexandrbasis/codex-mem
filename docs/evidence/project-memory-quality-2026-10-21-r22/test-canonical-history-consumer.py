"""Canonical cumulative-history selection at the actual helper accounting/export seam."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
sys.path.insert(0,str(ROOT))
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod
helper=load('r20_canonical_actual_helper',ROOT/'scripts/project_memory_consolidation_experiment.py')
toys=load('r20_canonical_toys',HERE/'test_public_fixture_contract_v2.py')
REPORT=ROOT/'docs/evidence/project-memory-quality-2026-10-21-r21/holdout/report.json'
REPORT_SHA='945747db507fb1e02740d92adcdd81f39ad1071fc4b8686c555e89fbf915b76d'


def usage(total,unknown=False):
    rows=[{'call_id':'known','status':'started','model':'gpt-6-luna'},
          {'call_id':'known','status':'completed','metrics':{'usage':{'status':'reported','tokens':{
              'input_tokens':total-2,'cached_input_tokens':0,'cache_write_input_tokens':0,'output_tokens':2,'reasoning_output_tokens':0,'total_tokens':total}}}}]
    if unknown:rows += [{'call_id':'unknown','status':'started','model':'gpt-6-luna'},
                        {'call_id':'unknown','status':'failed','metrics':{}}]
    return helper.usage(rows)


def pointer(path,field='cumulative_pilot_usage'):
    return {'report':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            **({'usage_field':field} if field is not None else {})}


class CanonicalHistoricalConsumerTests(unittest.TestCase):
    def test_canonical_report_selector_preserves_inherited_unknown_without_doublecount(self):
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/'canonical-report.json'
            helper.write(report,{'usage':usage(12),'cumulative_pilot_usage':usage(100,True)})
            current=usage(20)
            cumulative=helper.historical_accounting({'historical_spend':pointer(report)},current)['cumulative_pilot_usage']
            self.assertEqual(cumulative['attempted_calls'],3)
            self.assertEqual(cumulative['failed_calls'],1)
            self.assertEqual(cumulative['reported_tokens_lower_bound']['total_tokens'],120)
            self.assertFalse(cumulative['token_accounting_complete'])
            self.assertFalse(cumulative['pricing_complete'])
            self.assertIsNone(cumulative['tokens']['total_tokens'])
            legacy=helper.historical_accounting({'historical_spend':pointer(report,None)},current)['cumulative_pilot_usage']
            self.assertEqual(legacy['attempted_calls'],2)
            self.assertEqual(legacy['tokens']['total_tokens'],32)
            self.assertIsNone(cumulative['cost_usd'])

    def test_unknown_or_missing_selected_usage_field_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            report=Path(directory)/'report.json';helper.write(report,{'usage':usage(12)})
            for field in ('other','',False,[],None):
                selected=pointer(report);selected['usage_field']=field
                with self.subTest(field=field),self.assertRaises(ValueError):
                    helper.historical_accounting({'historical_spend':selected},helper.usage([]))
            with self.assertRaises((ValueError,KeyError)):
                helper.historical_accounting({'historical_spend':pointer(report)},helper.usage([]))

    def test_exact_r19_report_is_cumulative_not_current_only(self):
        self.assertEqual(hashlib.sha256(REPORT.read_bytes()).hexdigest(),REPORT_SHA)
        report=json.loads(REPORT.read_text());prior=report['historical_usage'];current=report['usage']
        self.assertEqual(current['attempted_calls'],55)
        self.assertEqual(current['reported_tokens_lower_bound']['total_tokens'],770655)
        bound={k:prior['reported_tokens_lower_bound'][k]+current['reported_tokens_lower_bound'][k]
               for k in current['reported_tokens_lower_bound']}
        self.assertEqual(bound,{'input_tokens':58495159,'cached_input_tokens':2749696,'output_tokens':2068956,'total_tokens':60564115})
        result=helper.historical_accounting({'historical_spend':pointer(REPORT)},helper.usage([]))['cumulative_pilot_usage']
        self.assertEqual(result['attempted_calls'],3805)
        self.assertEqual(result['calls_with_reported_tokens'],3803)
        self.assertEqual(result['calls_with_unknown_or_partial_tokens'],2)
        self.assertEqual(result['reported_tokens_lower_bound'],bound)
        self.assertFalse(result['token_accounting_complete'])
        self.assertFalse(result['pricing_complete'])
        self.assertIsNone(result['tokens']['total_tokens'])

    def test_actual_aggregate_and_export_keep_selected_cumulative_history_all216slots(self):
        fixture,gold=toys.toy()
        protocol=json.loads((ROOT/'docs/evidence/project-memory-quality-2026-10-21-r16/protocol.json').read_text())
        protocol['historical_spend']=pointer(REPORT)
        protocol['regeneration_repeat_history_ids']=['toy_0'];protocol['reader_repeat_history_ids']=['toy_1']
        with tempfile.TemporaryDirectory(prefix='r20-NONEXPERIMENT-') as directory:
            run=Path(directory);helper.write(run/'run.json',{'started_at':1,'budget_run':str(run)})
            gold_path=run/'synthetic-gold.json';helper.write(gold_path,gold)
            report=helper.aggregate(types.SimpleNamespace(run=run),fixture,protocol)
            self.assertEqual(report['decision'],'incomplete')
            self.assertEqual(report['cumulative_pilot_usage']['attempted_calls'],3805)
            target=run/'synthetic-export'
            result=helper.export_evidence(types.SimpleNamespace(run=run,destination=target,gold=gold_path),fixture,protocol)
            self.assertEqual(result['question_records'],96)
            attempts=[attempt for row in helper.read(target/'per-question.json')['questions'] for attempt in row['attempts']]
            self.assertEqual(len(attempts),216)
            self.assertTrue(all(a['status']=='missing_counts_as_failure' for a in attempts))
            exported=helper.read(target/'report.json')
            self.assertEqual(exported['cumulative_pilot_usage']['reported_tokens_lower_bound']['total_tokens'],60564115)
            self.assertEqual((target/'historical-report.json').read_bytes(),REPORT.read_bytes())
            self.assertFalse((run/'calls.jsonl').exists())
            self.assertFalse(list(run.rglob('*.sqlite*')))

if __name__=='__main__':unittest.main()
