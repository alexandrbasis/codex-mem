"""Offline checks for experiment accounting and acceptance gates."""
import importlib.util
from pathlib import Path
import tempfile
import types
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))

SCRIPT = Path(__file__).resolve().parents[1] / 'project_memory_consolidation_experiment.py'
SPEC = importlib.util.spec_from_file_location('experiment', SCRIPT)
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


def tokens(total=100):
    return dict(input_tokens=total-10, cached_input_tokens=0, cache_write_input_tokens=0,
                output_tokens=10, reasoning_output_tokens=0, total_tokens=total)


def receipt(status='reported', total=100):
    return {'usage': {'status':status, 'tokens':tokens(total)}}


def model_call(call_id, phase, variant, total=100, run_id='primary', repeat=0):
    base=dict(call_id=call_id,phase=phase,variant=variant,history_id='fiction-01',run_id=run_id,repeat=repeat,prompt_version=experiment.PROMPT_VERSION)
    return [{**base,'status':'started'},{**base,'status':'completed','metrics':receipt(total=total)}]


class ProtocolTests(unittest.TestCase):
    def test_partial_or_invalid_receipts_cannot_unlock_next_call(self):
        for metrics in (receipt('partial'), receipt('unavailable'), {'usage':{'status':'reported','tokens':{'total_tokens':10}}}):
            rows=[{'status':'started'},{'status':'failed','metrics':metrics}]
            self.assertFalse(experiment.usage(rows)['token_accounting_complete'])
        self.assertFalse(experiment.usage([{'status':'started'}])['token_accounting_complete'])
        self.assertTrue(experiment.usage([{'status':'started'},{'status':'completed','metrics':receipt()}])['token_accounting_complete'])

    def test_shared_visibility_decodes_xml_and_preserves_source_pairing(self):
        contexts=[{'memory':'PROJECT p\n<project-memory><source source_id="first"><quote_excerpt>Only first.</quote_excerpt></source></project-memory>'},
                  {'memory':'PROJECT p\n<project-memory><source source_id="other"><quote_excerpt>Keep A &amp; B &lt; 4.</quote_excerpt></source></project-memory>'}]
        visible=experiment.visible_source_quotes(contexts)
        citation=[{'source_id':'other','quote':'A & B < 4'}]
        self.assertTrue(experiment.citations_visible(citation,visible,{'other':'Keep A & B < 4.'}))
        self.assertFalse(experiment.citations_visible([{'source_id':'first','quote':'A & B < 4'}],visible,{'first':'A & B < 4'}))
        self.assertFalse(experiment.citations_visible([{'source_id':'other','quote':'This was never shown'}],visible,{'other':'This was never shown'}))

    def test_failed_candidate_remains_readable_but_fails_the_pipeline_gate(self):
        self.assertEqual(experiment.reader_pipeline_status('candidate',{'status':'failed'}),'failed')
        with self.assertRaises(ValueError):
            experiment.reader_pipeline_status('candidate',{'status':'running'})
        with tempfile.TemporaryDirectory() as temporary:
            run=Path(temporary)
            self.report(run)
            experiment.write(run/'candidate/fiction-01/consolidation.json',dict(status='failed',jobs=[dict(status='failed',job_kind='reconsideration')],accepted_merge_records=[],accepted_merge_count=0))
            value={'split':'holdout','histories':[{'id':'fiction-01'}]}
            report=experiment.aggregate(types.SimpleNamespace(run=run),value,{'regeneration_repeat_history_ids':[]})
            self.assertEqual(report['decision'],'fail')
            self.assertEqual(report['pipeline_failures'][0]['status'],'failed')
            self.assertEqual(report['variants']['candidate']['questions_graded'],6)

    def test_holdout_cannot_shrink_denominator(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'fixture.json'
            experiment.write(path,dict(schema_version=1,fictional=True,split='holdout',histories=[dict(id='fiction-05')]))
            with self.assertRaisesRegex(ValueError,'exactly 16'):
                experiment.fixture(path)

    def report(self, run, lost=False):
        experiment.write(run/'run.json',dict(budget_run=str(run)))
        fixture={'split':'holdout','histories':[{'id':'fiction-01'}]}
        protocol={'regeneration_repeat_history_ids':[]}
        for variant in ('baseline','candidate'):
            rows=[]
            for index in range(6):
                recall=not (lost and variant=='candidate' and index==0)
                rows.append(dict(id=f'q{index}',full_supported_answer=recall,current_state_actionable=True,
                    constraints_obligations_reasons_recalled=recall,citations_supported=True,critical_errors=[],
                    empty_or_evasive=False,fact_recall=[dict(fact_index=0,recalled=recall)],category='constraint'))
            experiment.write(run/variant/'fiction-01/judge-0.json',dict(grades=rows))
            experiment.write(run/variant/'fiction-01/contexts.json',[dict(context_chars=100)])
        calls=[]
        calls+=model_call('a','generation','baseline',100)
        calls+=model_call('b','reader','baseline',100)
        calls+=model_call('c','reader','candidate',100)
        calls+=model_call('d','consolidation','candidate',100)
        calls+=model_call('e','reader','candidate',10000,repeat=1)
        calls+=model_call('f','consolidation','candidate',10000,run_id='replicates/regeneration-1')
        for row in calls:
            experiment.append(run/'calls.jsonl',row)
        return experiment.aggregate(types.SimpleNamespace(run=run),fixture,protocol)

    def test_equal_quality_with_extra_cost_is_not_improvement(self):
        with tempfile.TemporaryDirectory() as temporary:
            report=self.report(Path(temporary))
        self.assertEqual(report['pilot_gate'],'pass')
        self.assertEqual(report['decision'],'no_improvement')
        self.assertEqual(report['equivalent_complete_arm_tokens'],dict(baseline=200,candidate=300))
        self.assertEqual(report['usage']['tokens']['total_tokens'],20400)
        self.assertEqual(report['usage_repeats']['tokens']['total_tokens'],20000)

    def test_identical_shared_memory_answer_change_is_variation_stratum(self):
        with tempfile.TemporaryDirectory() as temporary:
            run=Path(temporary)
            self.report(run)
            for arm in ('baseline','candidate'):
                contexts=[{'id':f'q{i}','memory':'<project-memory><source source_id="same"><quote_excerpt>Stable evidence.</quote_excerpt></source></project-memory>','context_chars':100} for i in range(6)]
                answers=[{'id':f'q{i}','answer':'different' if arm=='candidate' and i==0 else 'same'} for i in range(6)]
                experiment.write(run/arm/'fiction-01/contexts.json',contexts)
                experiment.write(run/arm/'fiction-01/reader-0.json',{'output':{'answers':answers}})
            report=experiment.aggregate(types.SimpleNamespace(run=run),{'split':'holdout','histories':[{'id':'fiction-01'}]}, {'regeneration_repeat_history_ids':[]})
            attribution=report['memory_change_attribution']
            self.assertEqual(attribution['per_query_exact_memory_pairs'],{'equal':6,'different':0,'missing':0})
            self.assertEqual(attribution['shared_reader_memory_strata']['identical_shared_reader_memory']['answer_changed'],1)
            self.assertTrue(attribution['history_results'][0]['shared_reader_memory_exactly_equal'])

    def test_failed_regeneration_pipeline_blocks_safe_repeat_answers(self):
        with tempfile.TemporaryDirectory() as temporary:
            run=Path(temporary)
            self.report(run)
            for arm in ('baseline','candidate'):
                directory=run/'replicates/regeneration-1'/arm/'fiction-01'
                experiment.write(directory/'judge-0.json',experiment.read(run/arm/'fiction-01/judge-0.json'))
                experiment.write(directory/('consolidation.json' if arm=='candidate' else 'generation.json'),
                    {'status':'failed' if arm=='candidate' else 'complete'})
            report=experiment.aggregate(types.SimpleNamespace(run=run),{'split':'holdout','histories':[{'id':'fiction-01'}]},
                {'regeneration_repeat_history_ids':['fiction-01']})
            self.assertTrue(report['repeats_complete'])
            self.assertEqual(report['candidate_repeat_critical_errors'],0)
            self.assertEqual(report['decision'],'fail')
            self.assertTrue(any(p.get('run_id')=='regeneration-1' and p['status']=='failed' for p in report['pipeline_failures']))

    def test_a_paired_constraint_loss_fails_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            report=self.report(Path(temporary),lost=True)
        self.assertEqual(report['decision'],'fail')
        self.assertFalse(report['constraints_preserved'])
        self.assertEqual(report['paired_fact_losses'][0]['fact_index'],0)


if __name__=='__main__':
    unittest.main()
