"""Public invented catalog examples test ownership, not model semantic quality."""
import copy
import unittest

from codex_mem import summary_support as support
from tests.test_summary_support import supported_review, support_rows
from tests.test_summary_support_contract import schema_accepts


class SupportRowOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.summary = dict(title='Book catalog report', request='Catalog the donated books.',
            completed='The librarian reported recording the donated books.',
            notes='The report records the catalog update.')
        self.note = dict(title='Catalog finding', body='The report describes donated books.', tags=['catalog'])
        self.claim = dict(sources=[dict(id='catalog-report', source='hook:Stop',
            body='The librarian reported recording the donated books in the catalog.')], context=[])
        self.prompt, self.schema, self.payload = support.prepare(self.summary, self.claim, [self.note])
        self.output = supported_review(self.payload)['output']

    def row(self, unit):
        return next(row for row in support_rows(self.output) if row['unit_id'] == unit)

    def test_schema_rejects_valid_span_id_owned_by_another_outcome_field(self):
        row = self.row('completed')
        row['candidate_span_ids'] = ['title:1']
        with self.assertRaises(support.SupportError) as error:
            support.validate(self.output, self.payload)
        self.assertEqual('unknown_span', error.exception.subtype)
        self.assertFalse(schema_accepts(self.output, self.schema))

    def test_schema_disallows_bindings_outside_each_unit_category(self):
        cases = [
            ('request', dict(target='other', report_span_ids=[], properties=[])),
            ('note:0:body', dict(target='report_text', report_span_ids=['current1:1'], properties=['content'])),
            ('notes', dict(target='outside_outcome', report_span_ids=[], properties=[])),
            ('completed', dict(target='other', report_span_ids=['current1:1'], properties=['content'])),
            ('completed', dict(target='reported_content', report_span_ids=['current1:1'], properties=['language'])),
            ('completed', dict(target='report_text', report_span_ids=[], properties=['content'])),
        ]
        for unit, binding in cases:
            with self.subTest(unit=unit, binding=binding):
                output = copy.deepcopy(self.output)
                next(row for row in support_rows(output) if row['unit_id'] == unit)['report_binding'] = binding
                with self.assertRaises(support.SupportError) as error:
                    support.validate(output, self.payload)
                self.assertEqual('verdict', error.exception.subtype)
                self.assertFalse(schema_accepts(output, self.schema))

    def test_same_owner_report_bindings_and_excluded_empty_arrays_remain_valid(self):
        for target, anchors, properties in [
            ('report_text', ['current1:1'], ['language', 'organization', 'content']),
            ('reported_content', ['current1:1'], ['content']),
            ('other', [], []), ('unresolved', [], []),
        ]:
            with self.subTest(target=target):
                output = copy.deepcopy(self.output)
                row = next(row for row in support_rows(output) if row['unit_id'] == 'completed')
                row['report_binding'] = dict(target=target, report_span_ids=anchors, properties=properties)
                self.assertTrue(schema_accepts(output, self.schema))
                review = support.validate(output, self.payload)
                self.assertEqual(('completed:1',), next(c for c in review.claims if c.unit_id == 'completed').candidate_span_ids)
                self.assertEqual(('insufficient_context',) if target == 'unresolved' else (),
                                 tuple(issue['verdict'] for issue in review.issues))
        for unit in ('request', 'note:0:title', 'note:0:body', 'note:0:tags'):
            row = self.row(unit)
            self.assertEqual([], row['candidate_span_ids'])
            self.assertEqual(dict(target='outside_outcome', report_span_ids=[], properties=[]), row['report_binding'])
        self.assertTrue(schema_accepts(self.output, self.schema))
        self.assertEqual((), support.validate(self.output, self.payload).issues)

    def test_note_cannot_borrow_summary_span_or_exclusion_omit_all_ownership(self):
        row = self.row('note:0:body')
        row['candidate_span_ids'] = ['notes:1']
        with self.assertRaises(support.SupportError) as error:
            support.validate(self.output, self.payload)
        self.assertEqual('unknown_span', error.exception.subtype)
        self.assertFalse(schema_accepts(self.output, self.schema))
        row['candidate_span_ids'] = []
        self.row('completed')['candidate_span_ids'] = []
        with self.assertRaises(support.SupportError) as error:
            support.validate(self.output, self.payload)
        self.assertEqual('verdict', error.exception.subtype)
        self.assertFalse(schema_accepts(self.output, self.schema))

    def test_local_validator_still_owns_duplicate_and_evidence_subset_checks(self):
        for field in ('candidate_span_ids', 'evidence_span_ids', 'report_span_ids', 'properties'):
            with self.subTest(field=field):
                output = copy.deepcopy(self.output)
                row = next(row for row in support_rows(output) if row['unit_id'] == 'completed')
                target = row['report_binding'] if field in ('report_span_ids', 'properties') else row
                target[field] = [target[field][0]] * 2
                self.assertTrue(schema_accepts(output, self.schema))
                with self.assertRaises(support.SupportError) as error:
                    support.validate(output, self.payload)
                self.assertEqual('duplicate_reference', error.exception.subtype)
        self.row('completed')['evidence_span_ids'] = []
        self.row('completed')['verdict'] = 'unsupported'
        self.assertTrue(schema_accepts(self.output, self.schema))
        with self.assertRaises(support.SupportError) as error:
            support.validate(self.output, self.payload)
        self.assertEqual('verdict', error.exception.subtype)

    def test_note_only_and_no_report_catalog_resolve_without_invalid_empty_enums(self):
        for summary, notes, source in [(None, [self.note], 'hook:PostToolUse'),
                                       (self.summary, [], 'hook:UserPromptSubmit')]:
            with self.subTest(note_only=summary is None):
                claim = dict(sources=[dict(id='catalog-request', source=source, body='Catalog the donated books.')])
                _, schema, payload = support.prepare(summary, claim, notes)
                output = supported_review(payload)['output']
                self.assertEqual({}, payload['report_catalog'])
                self.assertTrue(schema_accepts(output, schema))
                self.assertEqual((), support.validate(output, payload).issues)
                row = next(row for row in support_rows(output) if row['unit_id'] == ('note:0:body' if summary is None else 'completed'))
                row['report_binding'] = dict(target='report_text', report_span_ids=[], properties=['content'])
                self.assertFalse(schema_accepts(output, schema))


class SupportRowPublicTests(unittest.TestCase):
    from tests.test_summary_support import SummarySupportTests
    setUp = SummarySupportTests.setUp
    stored = SummarySupportTests.stored

    def test_wrong_category_binding_fails_atomically_without_source_rewrite(self):
        from codex_mem.processor import process_pending
        from codex_mem.store import Store
        from tests.test_summary_support import receipt
        body = 'The librarian reported recording the donated books in the catalog.'
        with Store(self.data) as store:
            raw = store.remember(self.project, 'Catalog report', body, source='hook:Stop', session_id='session')
        candidate = dict(disposition='processed', notes=[dict(title='Catalog finding', body=body, tags=['catalog'], source_ids=[raw['id']])],
            session_summary=dict(title='Catalog report', request=None, investigated='', learned='', completed=body,
                                 next_steps='', notes='The report describes the book catalog.', source_ids=[raw['id']]))
        seen = []
        def review(payload):
            seen.append(copy.deepcopy(payload))
            result = supported_review(payload)
            row = next(row for row in support_rows(result['output']) if row['unit_id'] == 'note:0:body')
            row['report_binding'] = dict(target='other', report_span_ids=[], properties=[])
            return result
        def generate(request):
            output = copy.deepcopy(candidate)
            output['session_summary']['source_ids'] = [source['id'] for source in request['sources']]
            output['notes'][0]['source_ids'] = list(output['session_summary']['source_ids'])
            return receipt(output)
        self.raw = raw
        result = process_pending(self.project, self.data, runner=generate, summary_reviewer=review)
        self.assertEqual('summary_review_malformed', result.get('reason_code'), result)
        self.assertEqual('verdict', result['summary_support'][0]['invalid_subtype'])
        outputs, attempts, saved = self.stored(result)
        self.assertEqual([], outputs)
        self.assertEqual(1, len(seen))
        with Store(self.data) as store:
            saved = store.get(self.project, [raw['id']])[0]
        self.assertEqual(body, saved['body'])
        self.assertIsNone(saved['superseded_by'])
