"""Authored invalid replies test safe diagnostics, not semantic review quality."""
import copy
import unittest

from codex_mem import summary_support as support
from tests.test_summary_support import supported_review
from tests.support_contract_fixtures import bind_report


class SupportDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.summary = dict(completed='The curator reported scanning the map.')
        self.claim = dict(sources=[dict(id='public-report', source='hook:Stop',
            body='The curator reported scanning the map.'), dict(id='public-request',
            source='hook:UserPromptSubmit', body='Please scan the map.')])
        _, _, self.payload = support.prepare(self.summary, self.claim)
        self.output = supported_review(self.payload)['output']

    def test_missing_evidence_is_rejected_and_old_subset_diagnostic_stays_readable(self):
        output = copy.deepcopy(self.output)
        output['units']['completed'][0]['citations'] = []
        with self.assertRaises(support.SupportError) as error:
            support.validate(output, self.payload)
        self.assertEqual(('verdict_requires_evidence','completed'),
                         (error.exception.rule_code,error.exception.unit_id))
        historical = support.diagnostic(self.summary,self.claim,'invalid','verdict',
            version='summary-support-v6',rule_code='report_anchor_not_in_evidence',unit_id='completed')
        self.assertEqual(historical,support.safe_diagnostic(historical))


    def test_verdict_checks_identify_the_fixed_rule_without_echoing_invalid_values(self):
        cases = [
            ('unknown_verdict', dict(verdict='PRIVATE INVALID VERDICT')),
            ('unknown_report_target', dict(report_binding=dict(target='PRIVATE TARGET', report_span_ids=[], properties=[]))),
            ('outcome_requires_candidate_span', dict(candidate_span_ids=[])),
            ('outcome_requires_report_target', dict(report_binding=dict(target='outside_outcome', report_span_ids=[], properties=[]))),
            ('nonreport_binding_has_anchors', dict(report_binding=dict(target='other', report_span_ids=['current1:1'], properties=[]))),
            ('nonreport_binding_has_properties', dict(report_binding=dict(target='unresolved', report_span_ids=[], properties=['content']))),
            ('report_requires_anchor', dict(report_binding=dict(target='report_text', report_span_ids=[], properties=['content']))),
            ('report_requires_property', dict(report_binding=dict(target='report_text', report_span_ids=['current1:1'], properties=[]))),
            ('reported_content_requires_content', dict(report_binding=dict(target='reported_content', report_span_ids=['current1:1'], properties=['language']))),
        ]
        for expected_rule, change in cases:
            with self.subTest(rule=expected_rule):
                output = copy.deepcopy(self.output)
                row=output['units']['completed'][0]
                change=copy.deepcopy(change)
                binding=change.pop('report_binding',None)
                row.update(change)
                if binding is not None:
                    bind_report(row,binding['target'],binding['report_span_ids'],binding['properties'])
                with self.assertRaises(support.SupportError) as error:
                    support.validate(output, self.payload)
                self.assertEqual('verdict', str(error.exception))
                self.assertEqual((expected_rule, 'completed'),
                                 (error.exception.rule_code, error.exception.unit_id))

    def test_excluded_note_binding_uses_code_owned_note_field(self):
        _, _, payload = support.prepare(None, self.claim,
            [dict(title='Map report', body='The curator reported scanning a map.', tags=[])])
        output = supported_review(payload)['output']
        output['units']['note:0:body'][0]['report_binding']['target'] = 'other'
        with self.assertRaises(support.SupportError) as error:
            support.validate(output, payload)
        self.assertEqual(('outside_outcome_binding', 'note:0:body'),
                         (error.exception.rule_code, error.exception.unit_id))

    def test_historical_diagnostics_remain_unchanged_and_optional_details_are_strict(self):
        for version in ('summary-support-v1', 'summary-support-v5', support.VERSION):
            original = support.diagnostic(self.summary, self.claim, 'invalid', 'verdict', version=version)
            self.assertEqual(5, len(original))
            self.assertEqual(original, support.safe_diagnostic(original))
        detail = support.diagnostic(self.summary, self.claim, 'invalid', 'verdict',
            rule_code='verdict_requires_evidence', unit_id='completed')
        self.assertEqual(detail, support.safe_diagnostic(detail))
        for unit in ('title', 'request', 'investigated', 'learned', 'next_steps', 'notes',
                     'note:3:observation:files_modified', 'note:0:body'):
            self.assertEqual(unit, support.safe_diagnostic(dict(detail, unit_id=unit))['unit_id'])
        changes = [dict(rule_code='PRIVATE RULE'), dict(rule_code=['unknown_verdict']),
                   dict(unit_id='PRIVATE MODEL UNIT'), dict(unit_id='note:4:body'),
                   dict(unit_id='note:0:observation:body'), dict(unit_id='note:0:extra'),
                   dict(unit_id=None), dict(rule_code=None), dict(status='supported'),
                   dict(invalid_subtype='structure'), dict(version='summary-support-v5'),
                   dict(raw_text='PRIVATE REVIEWER TEXT')]
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaisesRegex(ValueError, '^invalid support diagnostic$'):
                    support.safe_diagnostic(dict(detail, **change))
        for missing in ('rule_code', 'unit_id'):
            partial = dict(detail)
            partial.pop(missing)
            with self.assertRaises(ValueError):
                support.safe_diagnostic(partial)

    def test_exception_metadata_cannot_copy_arbitrary_model_unit_or_rule(self):
        for changes in (dict(unit_id='PRIVATE MODEL UNIT'), dict(rule_code='PRIVATE RULE'),
                        dict(unit_id='note:4:body'), dict(rule_code=None)):
            args = dict(rule_code='verdict_requires_evidence', unit_id='completed')
            args.update(changes)
            with self.assertRaisesRegex(ValueError, '^invalid support diagnostic$'):
                support.SupportError('verdict', **args)


class SupportDiagnosticPersistenceTests(unittest.TestCase):
    from tests.test_summary_support import SummarySupportTests
    setUp = SummarySupportTests.setUp
    stored = SummarySupportTests.stored

    def test_invalid_rule_and_unit_survive_public_attempt_ledger_without_prose(self):
        import json
        from codex_mem.processor import process_pending
        from codex_mem.store import Store
        from tests.test_summary_support import receipt
        with Store(self.data) as store:
            self.raw = store.remember(self.project, 'Map report', 'The curator reported scanning a map.',
                source='hook:Stop', session_id='public-session')
        def generate(request):
            return receipt(dict(disposition='processed', notes=[], session_summary=dict(
                title='Map report', request='', investigated='', learned='',
                completed='The curator reported scanning a map.', next_steps='', notes='',
                source_ids=[request['sources'][0]['id']])))
        def review(request):
            result = supported_review(request)
            row = result['output']['units']['completed'][0]
            row.update(citations=[], candidate_assertion='PRIVATE REVIEWER TEXT')
            return result
        result = process_pending(self.project, self.data, runner=generate, summary_reviewer=review)
        self.assertEqual('summary_review_malformed', result.get('reason_code'), result)
        detail = result['summary_support'][0]
        self.assertEqual('verdict_requires_evidence', detail['rule_code'])
        self.assertEqual('completed', detail['unit_id'])
        outputs, attempts, raw = self.stored(result)
        self.assertEqual([], outputs)
        self.assertIsNone(raw['superseded_by'])
        calls = json.loads(attempts[0]['execution_calls'])
        self.assertEqual({'generation', 'summary_support_1'}, set(calls))
        self.assertEqual(detail, calls['summary_support_1']['review_diagnostic'])
        self.assertNotIn('PRIVATE REVIEWER TEXT', attempts[0]['execution_calls'])
        self.assertNotIn('PRIVATE REVIEWER TEXT', json.dumps(result))
