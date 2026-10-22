"""Actual unchanged consumer checks, using synthetic artifacts and no Store/native."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import types
import unittest

HERE=Path(__file__).parent
REPO=HERE.resolve().parents[2]

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

builder_tests=load('r18_builder_toys',HERE/'test-protocol-builder.py')
helper=load('r18_unchanged_helper',REPO/'scripts/project_memory_consolidation_experiment.py')
toy_author=load('r18_public_toy_author',HERE/'test_public_fixture_contract_v2.py')


class ActualConsumerCompatibilityTests(builder_tests.ProtocolBuilderTests):
    # Reuse only synthetic receipt setup, not a native/runtime test adapter.
    def setUp(self):
        super().setUp()
        self.config['repeats']={'regeneration':'toy_0','reader':'toy_1'}
        self.history=self.save('synthetic-historical-usage.json',{'usage':helper.usage([])})
        self.config['historical_spend']=self.history

    def protocol(self):
        return self.build()

    def test_emitted_history_pointer_is_actual_consumer_report_key(self):
        protocol=self.protocol()
        self.assertEqual(protocol['historical_spend'],{'report':self.history['path'],'sha256':self.history['sha256']})
        accounting=helper.historical_accounting(protocol,helper.usage([]))
        self.assertTrue(accounting['cumulative_pilot_usage']['token_accounting_complete'])
        self.assertEqual(accounting['cumulative_pilot_usage']['attempted_calls'],0)
        self.assertEqual(self.config['historical_spend'],self.history)

    def test_old_path_key_rejects_in_actual_consumer_without_fallback(self):
        protocol=self.protocol();protocol['historical_spend']=deepcopy(self.history)
        with self.assertRaises(KeyError) as error:
            helper.historical_accounting(protocol,helper.usage([]))
        self.assertEqual(error.exception.args,('report',))

    @staticmethod
    def calls(identifier, known=True):
        start={'call_id':identifier,'status':'started','model':'gpt-6-luna'}
        if not known:return [start]
        tokens={'input_tokens':12,'cached_input_tokens':0,'cache_write_input_tokens':0,
                'output_tokens':3,'reasoning_output_tokens':0,'total_tokens':15}
        return [start,{'call_id':identifier,'status':'completed','metrics':{'usage':{'status':'reported','tokens':tokens}}}]

    def test_real_accounting_preserves_cumulative_known_and_unknown_usage(self):
        self.config['historical_spend']=self.save('synthetic-known-history.json',{'usage':helper.usage(self.calls('old'))})
        protocol=self.protocol()
        total=helper.historical_accounting(protocol,helper.usage(self.calls('new')))['cumulative_pilot_usage']
        self.assertEqual(total['attempted_calls'],2)
        self.assertEqual(total['tokens']['total_tokens'],30)
        unknown=helper.historical_accounting(protocol,helper.usage(self.calls('unresolved',False)))['cumulative_pilot_usage']
        self.assertFalse(unknown['token_accounting_complete'])
        self.assertIsNone(unknown['tokens']['total_tokens'])
        self.assertEqual(unknown['attempted_calls'],2)
        self.assertEqual(unknown['unresolved_calls'],1)
        self.assertIsNone(unknown['cost_usd'])

    def test_actual_consumer_rejects_changed_report_or_missing_sha_without_normalization(self):
        protocol=self.protocol();Path(self.history['path']).write_text('{}')
        with self.assertRaisesRegex(ValueError,'historical spend report changed'):
            helper.historical_accounting(protocol,helper.usage([]))
        protocol['historical_spend'].pop('sha256')
        with self.assertRaises(KeyError):helper.historical_accounting(protocol,helper.usage([]))

    def test_actual_aggregate_and_canonical_export_keep_all_missing_planned_slots(self):
        protocol=self.protocol()
        fixture,gold=toy_author.toy()
        run=self.repo/'synthetic-run';run.mkdir()
        helper.write(run/'run.json',{'started_at':1,'budget_run':str(run)})
        gold_path=self.repo/'toy-gold-for-export.json';gold_path.write_text(json.dumps(gold))
        report=helper.aggregate(types.SimpleNamespace(run=run),fixture,protocol)
        self.assertEqual(report['decision'],'incomplete')
        for arm in ('baseline','candidate'):
            self.assertEqual(report['variants'][arm]['questions_planned'],96)
            self.assertEqual(report['variants'][arm]['questions_graded'],0)
        self.assertEqual(report['cumulative_pilot_usage']['attempted_calls'],0)
        target=self.repo/'canonical-synthetic-export'
        exported=helper.export_evidence(types.SimpleNamespace(run=run,destination=target,gold=gold_path),fixture,protocol)
        self.assertEqual(exported['decision'],'incomplete')
        self.assertEqual(exported['question_records'],96)
        records=json.loads((target/'per-question.json').read_text())['questions']
        attempts=[attempt for record in records for attempt in record['attempts']]
        self.assertEqual(len(attempts),216)
        self.assertTrue(all(attempt['status']=='missing_counts_as_failure' for attempt in attempts))
        self.assertTrue(all(attempt['answer'] is None and attempt['grade'] is None for attempt in attempts))
        self.assertEqual((target/'historical-report.json').read_bytes(),Path(self.history['path']).read_bytes())
        self.assertTrue((target/'fictional-run-evidence.tar.gz').is_file())
        self.assertTrue((target/'export-manifest.json').is_file())
        self.assertFalse(list(run.rglob('*.sqlite*')))
        self.assertFalse((run/'calls.jsonl').exists())

    def test_actual_profile_and_contract_consumers_accept_generated_sibling_fields(self):
        protocol=self.protocol()
        self.assertEqual(helper.phase_profile(protocol,'generation'),{'model':'gpt-6-luna','reasoning_effort':'medium'})
        self.assertEqual(helper.phase_profile(protocol,'reader'),{'model':'gpt-6.1-sol','reasoning_effort':'medium'})
        self.assertEqual(helper.phase_profile(protocol,'judge'),{'model':'gpt-6-astra','reasoning_effort':'medium'})
        self.assertTrue(helper.reader_isolated(protocol))
        self.assertTrue(helper.paired_reuse_enabled(protocol))
        self.assertEqual(helper.shared_generation_runtime(protocol),'candidate')
        self.assertIsInstance(helper.reader_prompt_version(protocol),str)
        self.assertIsInstance(helper.judge_prompt_version(protocol),str)
        self.assertEqual(set(protocol['prospective_author_contract']),{'module','sha256'})
        self.assertEqual(protocol['independent_review_contract']['metadata_receipt_basename'],'sealed-semantic-review-metadata.json')


if __name__=='__main__':unittest.main()
