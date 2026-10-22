"""Actual unchanged consumer checks, using synthetic artifacts and no Store/native."""
from copy import deepcopy
import importlib.util
import ast
import json
from pathlib import Path
import types
import sys
import unittest
from unittest.mock import patch

HERE=Path(__file__).parent
REPO=HERE.resolve().parents[2]
sys.path.insert(0,str(REPO))

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

builder_tests=load('r18_builder_toys',HERE/'test-protocol-builder.py')
helper=load('r18_unchanged_helper',REPO/'scripts/project_memory_consolidation_experiment.py')
toy_author=load('r18_public_toy_author',HERE/'test_public_fixture_contract_v2.py')


def protocol_accesses(path):
    """Inventory literal consumer accesses; aliases/dynamic keys stay explicit."""
    rows=[]
    def protocol_root(node):
        return isinstance(node,ast.Name) and node.id=='protocol' or isinstance(node,ast.Attribute) and node.attr=='protocol'
    def literal_path(node):
        if protocol_root(node):return []
        if isinstance(node,ast.Subscript) and isinstance(node.slice,ast.Constant) and isinstance(node.slice.value,str):
            prefix=literal_path(node.value)
            return None if prefix is None else prefix+[node.slice.value]
        return None
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node,ast.Subscript):
            keys=literal_path(node)
            if keys:rows.append({'path':keys,'kind':'required','line':node.lineno})
        elif isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='get' and protocol_root(node.func.value):
            if node.args and isinstance(node.args[0],ast.Constant) and isinstance(node.args[0].value,str):
                rows.append({'path':[node.args[0].value],'kind':'optional','line':node.lineno})
    return sorted(rows,key=lambda row:(row['line'],row['path']))


class ActualConsumerCompatibilityTests(unittest.TestCase):
    save=builder_tests.ProtocolBuilderTests.save
    refresh_sealed_metadata=builder_tests.ProtocolBuilderTests.refresh_sealed_metadata
    build=builder_tests.ProtocolBuilderTests.build
    # Reuse only synthetic receipt setup, not a native/runtime test adapter.
    def setUp(self):
        builder_tests.ProtocolBuilderTests.setUp(self)
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

    def test_reusable_smoke_uses_real_consumers_with_only_temporary_synthetic_run(self):
        smoke=load('r18_smoke_tool',HERE/'consumer-smoke.py')
        output=self.repo/'smoke-output'
        result=smoke.smoke(self.protocol(),output,source_root=self.repo)
        self.assertEqual(result['planned_missing_answer_records'],216)
        self.assertEqual(result['native_attempts'],0)
        self.assertFalse(result['actual_trial_clock_started'])
        self.assertFalse(result['fresh_author_inputs_opened'])
        self.assertTrue((output/'canonical-export/export-manifest.json').is_file())
        self.assertTrue((output/'consumer-smoke-receipt.json').is_file())
        with self.assertRaisesRegex(ValueError,'already_exists'):
            smoke.smoke(self.protocol(),output,source_root=self.repo)

    def test_smoke_cli_rejects_actual_run_and_sealed_output_before_read_or_mkdir(self):
        smoke=load('r18_smoke_output_guard',HERE/'consumer-smoke.py')
        targets=[Path('/private/tmp/codex-mem-quality-20261021-r18-experiment'),
                 self.repo/'never-open-sealed'/'consumer-output']
        for target in targets:
            with self.subTest(target=target):
                self.assertFalse(target.exists())
                argv=['smoke','--protocol',str(self.repo/'must-not-open.json'),
                      '--protocol-sha256','0'*64,'--output',str(target)]
                with patch.object(sys,'argv',argv),self.assertRaisesRegex(ValueError,'output_must_be_inside'):
                    smoke.main()
                self.assertFalse(target.exists())

    def test_root_final_history_receipt_is_accepted_by_unchanged_actual_consumer(self):
        history=HERE/'historical-spend.json'
        expected='5b2905e0d96e8a0fde73cb9c7a9f4d3d0c2a8d6e422891fd1b31aef690706cf1'
        import hashlib
        self.assertEqual(hashlib.sha256(history.read_bytes()).hexdigest(),expected)
        protocol={'historical_spend':{'report':str(history.resolve()),'sha256':expected}}
        accounting=helper.historical_accounting(protocol,helper.usage([]))
        self.assertEqual(accounting['cumulative_pilot_usage']['attempted_calls'],2823)
        self.assertEqual(accounting['cumulative_pilot_usage']['tokens']['total_tokens'],45532599)
        self.assertTrue(accounting['cumulative_pilot_usage']['token_accounting_complete'])
        self.assertIsNone(accounting['cumulative_pilot_usage']['cost_usd'])

    def test_all_literal_required_protocol_accesses_resolve_without_renaming(self):
        protocol=self.protocol()
        paths=[REPO/'scripts/project_memory_consolidation_experiment.py',HERE/'run-experiment.py',HERE/'run-public-preflight.py']
        for source in paths:
            for access in protocol_accesses(source):
                if access['kind']!='required':continue
                with self.subTest(source=source.name,path=access['path'],line=access['line']):
                    value=protocol
                    for key in access['path']:value=value[key]
        # Historical alias pointer is not a direct protocol AST subscript.
        # Exercise that owning consumer separately, rather than trusting AST.
        self.assertEqual(set(protocol['historical_spend']),{'report','sha256'})
        helper.historical_accounting(protocol,helper.usage([]))

    def test_actual_profile_and_contract_consumers_accept_generated_sibling_fields(self):
        protocol=self.protocol()
        self.assertEqual(helper.phase_profile(protocol,'generation'),{'model':'gpt-6-luna','reasoning_effort':'medium'})
        self.assertEqual(helper.phase_profile(protocol,'reader'),{'model':'gpt-6.1-sol','reasoning_effort':'medium'})
        self.assertEqual(helper.phase_profile(protocol,'judge'),{'model':'gpt-6-astra','reasoning_effort':'medium'})
        self.assertTrue(helper.reader_isolated(protocol))
        self.assertTrue(helper.paired_reuse_enabled(protocol))
        self.assertEqual(helper.shared_generation_runtime(protocol),'candidate')
        helper.assert_baseline_runtime(Path(protocol['baseline_runtime_root']),protocol)
        self.assertIsInstance(helper.reader_prompt_version(protocol),str)
        self.assertIsInstance(helper.judge_prompt_version(protocol),str)
        self.assertEqual(set(protocol['prospective_author_contract']),{'module','sha256'})
        self.assertEqual(protocol['independent_review_contract']['metadata_receipt_basename'],'sealed-semantic-review-metadata.json')


if __name__=='__main__':unittest.main()
