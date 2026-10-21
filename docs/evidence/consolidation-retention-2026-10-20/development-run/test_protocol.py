"""Offline checks for experiment accounting and acceptance gates."""
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
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
    def test_every_native_dispatch_reserves_its_full_wall_allowance(self):
        protocol = experiment.read(Path(__file__).resolve().parents[2] /
                                   'docs/evidence/consolidation-retention-2026-10-20/protocol.json')
        limits = protocol['limits']
        for remaining, blocked in ((limits['call_seconds'] - 1, True),
                                   (limits['call_seconds'] + 60, False)):
            with self.subTest(remaining=remaining), tempfile.TemporaryDirectory() as temporary:
                run = Path(temporary)
                experiment.write(run / 'run.json', {
                    'started_at': experiment.time.time() - limits['wall_seconds'] + remaining,
                    'budget_run': str(run),
                })
                capture = experiment.Capture(run, protocol, 'generation', 'baseline', 'fiction-01')
                with patch('codex_mem.processor.NativeProcessorRunner') as runner:
                    runner.return_value.return_value = {'metrics': receipt()}
                    if blocked:
                        with self.assertRaisesRegex(RuntimeError, 'experiment_wall_budget_exhausted'):
                            capture({'prompt': 'Fictional test only.'})
                        runner.assert_not_called()
                        self.assertFalse((run / 'calls.jsonl').exists())
                    else:
                        capture({'prompt': 'Fictional test only.'})
                        runner.assert_called_once_with(timeout=limits['call_seconds'])
                        self.assertEqual(experiment.usage(experiment.ledger_calls(
                            run / 'calls.jsonl'))['attempted_calls'], 1)

    def test_actual_renderer_pairs_complete_long_quote_with_its_original_source(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tests'))
        from test_project_memory_consolidation import ConsolidationTests
        from codex_mem.project_memory_retrieval import build_project_brief,render_project_brief
        from codex_mem.store import Store
        case=ConsolidationTests()
        case.setUp()
        try:
            suffix='but production was not contacted.'
            body='Task LUX-54: '+('local preview detail '*18)+suffix
            source=case.remember(body,source='hook:Stop')
            self.assertEqual(case.process(case.standalone)['status'],'processed')
            with Store(case.data) as store:
                wrong=store.remember(case.project/'other-project','Unrelated tool result',
                    'Other prefix. Invisible original tail.',source='hook:PostToolUse',session_id='other-session')
                memory=render_project_brief(build_project_brief(store,case.project,include_shadow=True,budget=6000),budget=6000)
            visible=experiment.visible_source_quotes([{'memory':memory}])
            bodies={source['id']:body,wrong['id']:'Other prefix. Invisible original tail.'}
            self.assertTrue(experiment.citations_visible([{'source_id':source['id'],'quote':suffix}],visible,bodies))
            self.assertFalse(experiment.citations_visible([{'source_id':wrong['id'],'quote':suffix}],visible,bodies))
            prose=('<project-memory><field name="summary" basis="model_inference"><derived_text>Invisible original tail.</derived_text>'
                '<source source_id="'+wrong['id']+'"><quote_excerpt>Other prefix.</quote_excerpt></source></field></project-memory>')
            borrowed=experiment.visible_source_quotes([{'memory':memory+prose}])
            self.assertFalse(experiment.citations_visible([{'source_id':wrong['id'],'quote':'Invisible original tail.'}],borrowed,bodies))
        finally:
            case.doCleanups()

    def test_prior_spend_is_hash_pinned_and_separate_from_current_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'historical-report.json'
            prior=experiment.usage(model_call('old','reader','baseline',100))
            experiment.write(path,{'usage':prior})
            import hashlib
            protocol={'historical_spend':{'report':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}
            current=experiment.usage(model_call('new','reader','candidate',200))
            accounting=experiment.historical_accounting(protocol,current)
            self.assertEqual(current['attempted_calls'],1)
            self.assertEqual(accounting['historical_usage']['tokens']['total_tokens'],100)
            self.assertEqual(accounting['cumulative_pilot_usage']['tokens']['total_tokens'],300)
            self.assertEqual(accounting['cumulative_pilot_usage']['attempted_calls'],2)
            self.assertIsNone(accounting['coding_research_overhead_tokens'])
            path.write_text('{}')
            with self.assertRaises(ValueError):
                experiment.historical_accounting(protocol,current)

    def test_export_pseudonymizes_diagnostics_but_preserves_fictional_turns_and_metrics(self):
        thread='11111111-1111-4111-8111-111111111111'
        turn='22222222-2222-4222-8222-222222222222'
        fictional='33333333-3333-4333-8333-333333333333'
        payload={'jobs':[{'worker_thread_id':thread,'worker_turn_id':turn}],
            'evidence':{'thread_start':{'thread_id':thread},'turnStarted':{'threadId':thread,'turnId':turn}},
            'source_records':[{'turn_id':fictional,'source_id':'fictional-source','body':'7 passed locally.'}],
            'output':{'answer':'Fictional session-1 reports 7 passed locally.'},
            'metrics':{**receipt(total=100),'evidence':{'thread_start':{'thread_id':thread},
                'turn_started':{'thread_id':thread,'turn_id':turn}}}}
        members={'receipt.json':json.dumps(payload).encode(),'calls.jsonl':b'unchanged ledger\n'}
        cleaned,note=experiment.sanitize_export_members(members)
        value=json.loads(cleaned['receipt.json'])
        self.assertEqual(value['source_records'],payload['source_records'])
        self.assertEqual(value['metrics']['usage'],payload['metrics']['usage'])
        self.assertEqual(value['output'],payload['output'])
        self.assertEqual(value['jobs'][0]['worker_thread_id'],value['evidence']['thread_start']['thread_id'])
        self.assertEqual(value['jobs'][0]['worker_turn_id'],value['evidence']['turnStarted']['turnId'])
        self.assertEqual(value['jobs'][0]['worker_turn_id'],value['metrics']['evidence']['turn_started']['turn_id'])
        self.assertNotIn(thread,cleaned['receipt.json'].decode())
        self.assertNotIn(turn,cleaned['receipt.json'].decode())
        self.assertEqual(cleaned['calls.jsonl'],members['calls.jsonl'])
        self.assertNotIn(thread,json.dumps(note))
        self.assertEqual(cleaned,experiment.sanitize_export_members(members)[0])
        archive=experiment.export_archive_bytes(cleaned)
        self.assertEqual(archive[4:8],b'\0\0\0\0')
        self.assertEqual(archive,experiment.export_archive_bytes(cleaned))

    def test_ledger_rejects_duplicate_or_mismatched_attempt_receipts(self):
        rows=model_call('a','reader','baseline')
        experiment.validate_ledger(rows)
        for invalid in (rows+[rows[1]], [rows[1]],
                        [rows[0],{**rows[1],'variant':'candidate'}],
                        [rows[0],rows[0]]):
            with self.assertRaises(ValueError):
                experiment.validate_ledger(invalid)

    def test_retrieval_timing_is_saved_without_entering_memory_payload(self):
        from codex_mem.store import Store
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with Store(root/'data'):
                pass
            context=experiment.retrieve_question(root/'data',{'a':str(root/'project')},
                {'id':'q1','question':'What remains open?','project_keys':['a']},6000,root/'retrieval-timings.jsonl')
            self.assertEqual(set(context),{'id','question','memory','context_chars','context_sha256'})
            self.assertLessEqual(context['context_chars'],6000)
            timing=experiment.calls(root/'retrieval-timings.jsonl')[0]
            self.assertEqual(timing['question_id'],'q1')
            self.assertEqual(timing['status'],'completed')
            self.assertEqual(timing['projects'][0]['project_key'],'a')
            self.assertGreaterEqual(timing['wall_ms'],0)
            self.assertGreaterEqual(timing['projects'][0]['wall_ms'],0)
            self.assertEqual(timing['context_sha256'],context['context_sha256'])

    def test_tool_capture_rejects_scope_overrides_and_ambiguous_exit_status(self):
        capture={'tool_name':'Bash','tool_input':{'command':'pytest local_spec.py'},
                 'tool_response':{'exit_code':0,'output':'7 passed locally.'}}
        experiment.validate_tool_capture({'type':'tool','tool_capture':capture})
        for bad in ({**capture,'session_id':'wrong-session'},
                    {**capture,'tool_response':{'exit_code':True,'output':'7 passed locally.'}}):
            with self.assertRaises(ValueError):
                experiment.validate_tool_capture({'type':'tool','tool_capture':bad})
        with self.assertRaises(ValueError):
            experiment.validate_tool_capture({'type':'assistant','tool_capture':capture})

    def test_tool_fixture_preserves_execution_and_project_session_scope(self):
        from codex_mem.store import Store
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            project=root/'fictional-project'
            other=root/'other-project'
            event={'id':'tool-event','type':'tool','text':'Command: pytest local_spec.py\nExit code: 0\nOutput: 7 passed locally. No deployment ran.',
                'tool_capture':{'tool_name':'Bash','tool_input':{'command':'pytest local_spec.py'},
                                'tool_response':{'exit_code':0,'output':'7 passed locally. No deployment ran.'}}}
            with Store(root/'data') as store:
                stored=experiment.capture_event(store,project,'fictional-session',event)
                record=store.get(project,[stored['id']])[0]
                raw=store.get_tool_uses(project,session_id='fictional-session')
                self.assertEqual(record['provenance']['record_role'],'tool_record')
                self.assertEqual(record['body'],event['text'])
                self.assertEqual(len(raw),1)
                self.assertEqual(json.loads(raw[0]['tool_input']),event['tool_capture']['tool_input'])
                self.assertEqual(json.loads(raw[0]['tool_response']),event['tool_capture']['tool_response'])
                self.assertEqual(raw[0]['turn_id'],'tool-event')
                self.assertEqual(store.get_tool_uses(other,session_id='fictional-session'),[])
                self.assertEqual(store.get_tool_uses(project,session_id='another-session'),[])

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
