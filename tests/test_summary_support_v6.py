"""Public keyed-owner examples prove transport/validation, never model meaning."""
import copy
import unittest

from codex_mem import summary_support as support
from tests.test_summary_support_contract import schema_accepts


class KeyedSupportContractTests(unittest.TestCase):
    def setUp(self):
        notes = [dict(title='Museum digitization', body='The conservator reported scanning a map.', tags=[]),
                 dict(title='Station timetable', body='The clerk reported drafting a timetable.', tags=[])]
        self.claim = dict(sources=[dict(id='public-source', source='hook:Stop',
            body='The conservator reported scanning a map. The clerk reported drafting a timetable.')])
        _, self.schema, self.payload = support.prepare(None, self.claim, notes)

    def reply(self):
        return dict(version=support.VERSION, units={unit:[dict(unit_id=unit, coverage='complete',
            candidate_assertion='An attributed public finding.', evidence_assertion='The supplied report states the finding.',
            value_comparison='Authored same value.', attribution_comparison='Authored same speaker.',
            scope_comparison='Authored same scope.', verdict='supported', citations=[dict(span_id='current1:1', role='evidence')],
            candidate_span_ids=[], report_binding=dict(target='outside_outcome', properties=[]))]
            for unit in self.payload['units']})

    def test_missing_owner_cannot_be_hidden_by_sufficient_duplicated_comparisons(self):
        complete = self.reply()
        missing = copy.deepcopy(complete)
        unit = next(iter(missing['units']))
        missing['units'] = {unit:missing['units'][unit]*len(self.payload['units'])}
        with self.assertRaises(support.SupportError) as error:
            support.validate(missing, self.payload)
        self.assertEqual('unit_coverage', error.exception.subtype)
        self.assertFalse(schema_accepts(missing, self.schema))
        self.assertTrue(schema_accepts(complete, self.schema))
        self.assertEqual((), support.validate(complete, self.payload).issues)

    def test_exact_owner_keys_array_bounds_and_inner_owner_mismatch_fail_closed(self):
        first = next(iter(self.payload['units']))
        second = list(self.payload['units'])[1]
        changes = [
            ('extra owner', lambda v:v['units'].update(extra=v['units'][first]), 'unit_coverage'),
            ('empty owner', lambda v:v['units'].update({first:[]}), 'bounds'),
            ('non-array owner', lambda v:v['units'].update({first:{}}), 'bounds'),
            ('inner owner mismatch', lambda v:v['units'][first][0].update(unit_id=second), 'unit_coverage'),
            ('ninth comparison', lambda v:v['units'].update({first:v['units'][first]*9}), 'bounds'),
        ]
        for name,change,subtype in changes:
            with self.subTest(case=name):
                output=self.reply();change(output)
                with self.assertRaises(support.SupportError) as error:support.validate(output,self.payload)
                self.assertEqual(subtype,error.exception.subtype)
                self.assertFalse(schema_accepts(output,self.schema))
        complete=self.reply()
        complete['units'][first] *= 8
        complete['units'][second] *= 2
        self.assertTrue(schema_accepts(complete,self.schema))
        self.assertEqual((),support.validate(complete,self.payload).issues)

    def test_payload_order_owns_claim_ids_without_reassigning_reversed_response_groups(self):
        summary=dict(title='Museum report',completed='The conservator reported scanning a map.')
        _,schema,payload=support.prepare(summary,self.claim)
        from tests.test_summary_support import supported_review
        output=supported_review(payload)['output']
        output['units']['completed'].append(copy.deepcopy(output['units']['completed'][0]))
        output['units']=dict(reversed(list(output['units'].items())))
        self.assertTrue(schema_accepts(output,schema))
        review=support.validate(output,payload)
        self.assertEqual(['title#1','completed#1','completed#2'],[claim.id for claim in review.claims])
        self.assertEqual(list(reversed(list(payload['units']))),list(output['units']))

    def test_historical_v5_wire_is_rejected_but_v5_and_v6_diagnostics_remain_readable(self):
        import json
        from pathlib import Path
        fixture=Path(__file__).parent/'fixtures/binding_routing_case.json'
        historical=json.loads(fixture.read_text())['support']
        self.assertEqual('summary-support-v5',historical['version'])
        with self.assertRaises(support.SupportError) as error:support.validate(historical,self.payload)
        self.assertEqual('structure',error.exception.subtype)
        for version in ('summary-support-v5','summary-support-v7'):
            detail=support.diagnostic(self.payload['units'],self.claim,'invalid','unit_coverage',version=version)
            self.assertEqual(detail,support.safe_diagnostic(detail))
        self.assertEqual('summary-support-v7',support.diagnostic(self.payload['units'],self.claim)['version'])

    def test_duplicate_unit_keys_and_trailing_data_fail_in_both_existing_decoders(self):
        from unittest import mock
        from codex_mem.processor import _read_valid_final_output,ProcessorFailure
        from codex_mem.pi_runtime import _json
        import json
        raw=json.dumps(self.reply())
        for value in ('{"version":"summary-support-v6","units":{"note:0:title":[],"note:0:title":[]}}',raw+' trailing'):
            monitor=mock.Mock(thread_id='public-worker',turn_id='public-turn')
            monitor.final_agent_text.return_value=value
            for decoder in (lambda:_json(value),lambda:_read_valid_final_output(monitor)):
                with self.assertRaises(ProcessorFailure) as error:decoder()
                self.assertEqual('invalid_json',error.exception.reason_code)


class KeyedSupportPublicTests(unittest.TestCase):
    from tests.test_summary_support import SummarySupportTests
    setUp=SummarySupportTests.setUp
    stored=SummarySupportTests.stored

    def test_missing_owner_fails_atomically_without_review_repair_or_invented_usage(self):
        import json
        from unittest import mock
        from codex_mem.processor import process_pending
        from codex_mem.store import Store
        from tests.test_summary_support import supported_review,receipt
        body='A conservator reported scanning a map. A station clerk reported drafting a timetable.'
        with Store(self.data) as store:
            self.raw=store.remember(self.project,'Unrelated reported findings',body,source='hook:PostToolUse',session_id='public-session')
        def generate(request):
            return receipt(dict(disposition='processed',session_summary=None,notes=[
                dict(title='Museum finding',body='A conservator reported scanning a map.',tags=[],source_ids=[request['sources'][0]['id']]),
                dict(title='Station finding',body='A clerk reported drafting a timetable.',tags=[],source_ids=[request['sources'][0]['id']])]))
        def review(request):
            output=supported_review(request)['output'];first=next(iter(output['units']))
            output['units']={first:output['units'][first]*len(request['units'])}
            return receipt(output)
        generator=mock.Mock(side_effect=generate);reviewer=mock.Mock(side_effect=review)
        result=process_pending(self.project,self.data,runner=generator,summary_reviewer=reviewer)
        self.assertEqual('summary_review_malformed',result.get('reason_code'),result)
        generator.assert_called_once();reviewer.assert_called_once()
        self.assertEqual('unit_coverage',result['summary_support'][0]['invalid_subtype'])
        self.assertEqual('summary-support-v7',result['summary_support'][0]['version'])
        outputs,attempts,raw=self.stored(result)
        self.assertEqual([],outputs);self.assertIsNone(raw['superseded_by']);self.assertEqual(body,raw['body'])
        calls=json.loads(attempts[0]['execution_calls'])
        self.assertEqual({'generation','summary_support_1'},set(calls))
        self.assertEqual('summary-support-v7',calls['summary_support_1']['review_diagnostic']['version'])
        self.assertIsNone(attempts[0]['total_tokens'])
