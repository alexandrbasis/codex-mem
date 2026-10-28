"""Typed citation transport tests; authored comparisons do not prove semantics."""
import copy
import unittest
from codex_mem import summary_support as support
from tests.test_summary_support_contract import schema_accepts


class TypedCitationTests(unittest.TestCase):
    def setUp(self):
        self.summary = dict(title='Map scan report', completed='The assistant reported scanning a map.')
        self.claim = dict(sources=[dict(id='tool', source='hook:PostToolUse', body='A scan operation returned a result.'),
            dict(id='report', source='hook:Stop', body='I reported scanning a map.')])
        _, self.schema, self.payload = support.prepare(self.summary, self.claim)

    def reply(self):
        return dict(version='summary-support-v7', units={unit:[dict(unit_id=unit, coverage='complete',
            candidate_assertion='An attributed report of a scan.', evidence_assertion='Authored report comparison.',
            value_comparison='Same reported action.', attribution_comparison='Same reporting scope.',
            scope_comparison='Original report only.', verdict='supported',
            citations=[dict(span_id='current1:1', role='evidence'), dict(span_id='current2:1', role='report_anchor')],
            candidate_span_ids=[unit+':1'], report_binding=dict(target='reported_content', properties=['content']))]
            for unit in self.payload['units']})

    def test_single_selection_owns_both_evidence_and_report_binding(self):
        output = self.reply()
        before = copy.deepcopy(output)
        review = support.validate(output, self.payload)
        self.assertTrue(schema_accepts(output, self.schema))
        self.assertEqual((), review.issues)
        self.assertTrue(all(claim.report_span_ids == ('current2:1',) for claim in review.claims))
        self.assertEqual(before, output, 'Lowering must not mutate raw wire output')
        output['units']['completed'][0]['verdict'] = 'unsupported'
        issue = support.validate(output, self.payload).issues[0]
        self.assertEqual(['current1:1', 'current2:1'], issue['comparison']['evidence_span_ids'])

    def test_old_wire_and_duplicate_declarations_are_not_repaired(self):
        for old_version in ('summary-support-v6', 'summary-support-v7'):
            output = self.reply()
            output['version'] = old_version
            for rows in output['units'].values():
                for row in rows:
                    row.pop('citations')
                    row['evidence_span_ids'] = ['current1:1']
                    row['report_binding']['report_span_ids'] = ['current2:1']
            with self.subTest(version=old_version):
                with self.assertRaises(support.SupportError): support.validate(output, self.payload)
                self.assertFalse(schema_accepts(output, self.schema))

    def test_nonreport_bindings_cannot_select_report_anchor_citations(self):
        for target in ('other', 'unresolved'):
            with self.subTest(target=target):
                output = self.reply()
                row = output['units']['completed'][0]
                row['report_binding'] = dict(target=target, properties=[])
                with self.assertRaises(support.SupportError) as error:
                    support.validate(output, self.payload)
                self.assertEqual('nonreport_binding_has_anchors', error.exception.rule_code)
                self.assertFalse(schema_accepts(output, self.schema))
                row['citations'] = [dict(ref, role='evidence') for ref in row['citations']]
                self.assertTrue(schema_accepts(output, self.schema))
                review = support.validate(output, self.payload)
                self.assertEqual(('insufficient_context',) if target == 'unresolved' else (),
                                 tuple(issue['verdict'] for issue in review.issues))

    def test_invalid_citation_ownership_duplicates_and_bounds_remain_rejected(self):
        changes = [
            lambda r:r['citations'].append(dict(span_id='foreign:1', role='evidence')),
            lambda r:r['citations'][0].update(role='report_anchor'),
            lambda r:r['citations'].append(dict(span_id='current2:1', role='evidence')),
            lambda r:r.update(citations=r['citations']*7),
            lambda r:r['citations'][0].update(role='unknown'),
            lambda r:r['report_binding'].update(report_span_ids=['current2:1']),
            lambda r:r.update(candidate_span_ids=['title:1']),
        ]
        for change in changes:
            output=self.reply();change(output['units']['completed'][0])
            with self.subTest(change=change):
                with self.assertRaises(support.SupportError):support.validate(output,self.payload)

    def test_report_binding_branches_preserve_properties_and_anchor_ownership(self):
        for target, properties in (('reported_content', ['content']), ('report_text', ['language'])):
            with self.subTest(target=target):
                output = self.reply()
                row = output['units']['completed'][0]
                row['report_binding'] = dict(target=target, properties=properties)
                self.assertTrue(schema_accepts(output, self.schema))
                support.validate(output, self.payload)
                row['report_binding']['properties'] = []
                self.assertFalse(schema_accepts(output, self.schema))
                with self.assertRaises(support.SupportError): support.validate(output, self.payload)
                row['report_binding']['properties'] = properties
                row['citations'][1]['span_id'] = 'current1:1'
                self.assertFalse(schema_accepts(output, self.schema))
                with self.assertRaises(support.SupportError): support.validate(output, self.payload)
